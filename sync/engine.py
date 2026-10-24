import asyncio
import logging
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.engine import Engine

from apis.registry import NETWORK_APIS
from apis.types import Post
from config import (
    NETWORK_LIMITS,
    REPLY_FILTER_NETWORKS,
    SOURCE_ONLY_NETWORKS,
    TWITTER_FETCH_MAX_PAGES,
)
from db.accounts import Account, account_display_name, get_account, get_all_credentials, list_accounts
from db.helpers import validate_account_role
from db.projects import get_project, list_projects
from db.schema import projects
from db.deliveries import (
    Delivery, complete_delivery, create_delivery, load_deliveries, record_delivery_part,
)
from db.posts import discard_posts, load_posts, save_posts
from db.sync_state import (
    get_last_synced_at, get_mirrored_post_ids, get_sync_mappings, set_last_synced_at,
)
from utils.filters import exclude_source_only_posts, filter_own_threads
from utils.posts import sort_chronologically
from utils.thread_processor import build_outbound_posts, collect_ready_batch
from utils.urls import unwrap_posts_text

logger = logging.getLogger(__name__)


async def sync_account(
    engine: Engine,
    source: Account,
    all_accounts: list[Account],
    since: datetime | None = None,
    enforce_min_age: bool = True,
    min_age_minutes: int | None = None,
    post_delay_seconds: float = 0,
    errors: list[str] | None = None,
) -> int:
    source = get_account(engine, source.id)
    if source is None or source.sync_role == "destination_only":
        return 0
    with engine.connect() as connection:
        validate_account_role(
            connection, source.network, source.project_id, source.remote_id,
            source.sync_role, get_all_credentials(engine, source.id),
        )
    project = get_project(engine, source.project_id)
    if not project.enabled:
        return 0
    if min_age_minutes is None:
        min_age_minutes = project.min_age_minutes
    errors = errors if errors is not None else []

    destinations = [
        account for account in all_accounts
        if account.project_id == source.project_id
        and account.id != source.id and account.sync_role in ("source_and_destination", "destination_only")
    ]
    if not destinations:
        return 0

    api = NETWORK_APIS[source.network]
    source_name = account_display_name(source, engine)
    started_at = datetime.now(timezone.utc)
    last_sync = get_last_synced_at(engine, source.id)
    fetch_since = since or (
        last_sync - timedelta(hours=project.overlap_hours)
        if last_sync else started_at - timedelta(hours=project.initial_lookback_hours)
    )
    max_pages = None if since or source.network in SOURCE_ONLY_NETWORKS else TWITTER_FETCH_MAX_PAGES

    try:
        fetched = await api.fetch_posts(
            engine, source.id, since=fetch_since, include_replies=True, max_pages=max_pages,
        )
        with engine.begin() as connection:
            save_posts(connection, source.id, fetched)
        set_last_synced_at(engine, source.id, started_at)
    except Exception:
        errors.append(f"fetch:{source.id}")
        logger.exception("[%s] Fetch failed; processing saved posts", source_name)

    # Fetch can resolve a legacy remote identity; check isolation again before delivery.
    source = get_account(engine, source.id)
    if source is None or source.sync_role == "destination_only":
        return 0
    with engine.connect() as connection:
        validate_account_role(
            connection, source.network, source.project_id, source.remote_id,
            source.sync_role, get_all_credentials(engine, source.id),
        )
    cached = load_posts(engine, source.id)
    mirrored = get_mirrored_post_ids(engine, source.id)
    originals = [post for post in cached if post.id not in mirrored]
    posts = exclude_source_only_posts(originals)
    if source.network in REPLY_FILTER_NETWORKS:
        posts = filter_own_threads(posts)
    if since:
        posts = [post for post in posts if post.created_at >= since]

    conversations: dict[str, list[Post]] = defaultdict(list)
    for post in sort_chronologically(posts):
        conversations[post.conversation_id].append(post)

    mappings = get_sync_mappings(engine, source.id)
    pending = {
        (delivery.conversation_id, delivery.dest_account_id): delivery
        for delivery in load_deliveries(engine, source.id)
    }
    # Saved batches remain publishable even when the source stops returning their posts.
    for conversation_id, dest_id in pending:
        conversations.setdefault(conversation_id, [])

    synced = 0
    for conversation_id, thread in conversations.items():
        for dest in destinations:
            delivery = pending.get((conversation_id, dest.id))
            try:
                if delivery is None:
                    batch = collect_ready_batch(
                        thread,
                        is_synced=lambda post_id: (post_id, dest.id) in mappings,
                        enforce_min_age=enforce_min_age,
                        min_age_minutes=min_age_minutes,
                    )
                    if not batch:
                        continue

                    # URL expansion must not mutate shared source content or the inbox.
                    batch = [replace(post) for post in batch]
                    await unwrap_posts_text(batch)
                    limits = NETWORK_LIMITS[dest.network]
                    outbounds = (
                        NETWORK_APIS["instagram"].build_outbounds(batch, limits)
                        if source.network == "instagram" else build_outbound_posts(batch, limits)
                    )
                    if not outbounds:
                        continue

                    reply_ref = None
                    first_index = next(i for i, post in enumerate(thread) if post.id == batch[0].id)
                    if first_index:
                        reply_ref = mappings.get((thread[first_index - 1].id, dest.id))
                        if reply_ref and dest.network == "bluesky":
                            reply_ref = await NETWORK_APIS["bluesky"].resolve_reply_target(
                                engine, dest.id, reply_ref,
                            )
                    delivery = Delivery(source.id, dest.id, conversation_id, outbounds, [], reply_ref)
                    create_delivery(engine, delivery)

                for outbound in delivery.outbounds[len(delivery.dest_ids):]:
                    media_bytes = []
                    if dest.network != "threads":
                        for media in outbound.media:
                            media_bytes.append(await api.download_media(media, engine, source.id))
                    result = await NETWORK_APIS[dest.network].publish_outbound(
                        engine, dest.id, outbound, media_bytes, reply_to=delivery.reply_ref,
                    )
                    record_delivery_part(engine, delivery, result)
                    if post_delay_seconds:
                        await asyncio.sleep(post_delay_seconds)

                synced += complete_delivery(engine, delivery)
                pending.pop((conversation_id, dest.id), None)
                for outbound, dest_id in zip(delivery.outbounds, delivery.dest_ids):
                    for post_id in outbound.source_post_ids:
                        mappings[post_id, dest.id] = dest_id
            except Exception:
                errors.append(f"delivery:{source.id}:{dest.id}")
                logger.exception(
                    "[%s -> %s] Delivery failed; saved progress will be retried",
                    source_name, account_display_name(dest, engine),
                )

    # Keep incomplete threads and recent parents; retire old, fully delivered content.
    discard = []
    eligible_ids = {post.id for post in posts}
    for post in cached:
        if post.id in mirrored or (post.id not in eligible_ids and post.created_at < fetch_since and not since):
            discard.append(post.id)
    for conversation_id, thread in conversations.items():
        if not thread or any(post.created_at >= fetch_since for post in thread):
            continue
        if all((post.id, dest.id) in mappings for post in thread for dest in destinations):
            discard.extend(post.id for post in thread)
    discard_posts(engine, source.id, discard)
    return synced


async def run_sync(
    engine: Engine,
    since: datetime | None = None,
    enforce_min_age: bool = True,
    *, project_id: int | None = None, account_id: int | None = None,
) -> int:
    if account_id is not None:
        account = get_account(engine, account_id)
        if account is None or account.sync_role == "destination_only":
            raise ValueError("Select a source-enabled connection")
        if project_id is not None and account.project_id != project_id:
            raise ValueError("Connection belongs to another project")
        project_id = account.project_id
    selected = [get_project(engine, project_id)] if project_id is not None else list_projects(engine)
    total = 0
    for project in selected:
        if not project.enabled:
            continue
        started_at = datetime.now(timezone.utc)
        with engine.begin() as connection:
            connection.execute(update(projects).where(projects.c.id == project.id).values(
                last_sync_started_at=started_at, last_sync_finished_at=None,
                last_sync_count=0, last_sync_error_count=0,
            ))

        accounts = []
        count = 0
        errors = []
        for account in list_accounts(engine, project_id=project.id):
            try:
                with engine.connect() as connection:
                    validate_account_role(
                        connection, account.network, account.project_id, account.remote_id,
                        account.sync_role, get_all_credentials(engine, account.id),
                        allow_destination_only=True,
                    )
                accounts.append(account)
            except ValueError as exc:
                errors.append(f"configuration:{account.id}")
                logger.error("[%s / %s] %s", project.name, account.label, exc)
        for source in accounts:
            if source.sync_role == "destination_only" or (account_id is not None and source.id != account_id):
                continue
            try:
                count += await sync_account(
                    engine, source, accounts, since=since,
                    enforce_min_age=enforce_min_age and since is None,
                    post_delay_seconds=project.backfill_delay_seconds if since else 0,
                    errors=errors,
                )
            except Exception:
                errors.append(f"source:{source.id}")
                logger.exception("[%s / %s] Sync failed", project.name, account_display_name(source, engine))

        finished_at = datetime.now(timezone.utc)
        values = {
            "last_sync_finished_at": finished_at,
            "last_sync_count": count, "last_sync_error_count": len(errors),
        }
        if not errors:
            values["last_synced_at"] = finished_at
        with engine.begin() as connection:
            connection.execute(update(projects).where(projects.c.id == project.id).values(**values))
        total += count
    return total
