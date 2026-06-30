import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.engine import Engine

import apis.bluesky as bluesky_api
import apis.instagram as instagram_api
import apis.mastodon as mastodon_api
import apis.rss as rss_api
import apis.telegram as telegram_api
import apis.threads as threads_api
import apis.twitter as twitter_api
from apis.types import OutboundPost, Post
from apis.urls import unwrap_posts_text
from config import (
    BACKFILL_POST_DELAY_SECONDS,
    NETWORK_BLUESKY,
    NETWORK_INSTAGRAM,
    NETWORK_MASTODON,
    NETWORK_RSS,
    NETWORK_TELEGRAM,
    NETWORK_THREADS,
    NETWORK_TWITTER,
    POST_MIN_AGE_MINUTES,
    SOURCE_ONLY_NETWORKS,
    WATCH_INITIAL_LOOKBACK_HOURS,
    WATCH_MAX_PAGES,
    WATCH_OVERLAP_HOURS,
)
from db.accounts import Account, account_display_name, list_accounts
from db.sync_state import (
    get_dest_post_id,
    get_last_synced_at,
    get_mirrored_post_ids,
    is_synced,
    mark_synced,
    record_mirrored_post,
    set_last_synced_at,
)
from apis.types import sort_chronologically
from sync.thread_processor import (
    build_outbound_posts,
    collect_ready_batch,
    get_network_limits,
)

logger = logging.getLogger(__name__)

FETCH_HANDLERS = {
    NETWORK_TWITTER: twitter_api.fetch_posts,
    NETWORK_TELEGRAM: telegram_api.fetch_posts,
    NETWORK_MASTODON: mastodon_api.fetch_posts,
    NETWORK_THREADS: threads_api.fetch_posts,
    NETWORK_BLUESKY: bluesky_api.fetch_posts,
    NETWORK_RSS: rss_api.fetch_posts,
    NETWORK_INSTAGRAM: instagram_api.fetch_posts,
}
PUBLISH_HANDLERS = {
    NETWORK_TWITTER: twitter_api.publish_outbound,
    NETWORK_TELEGRAM: telegram_api.publish_outbound,
    NETWORK_MASTODON: mastodon_api.publish_outbound,
    NETWORK_THREADS: threads_api.publish_outbound,
    NETWORK_BLUESKY: bluesky_api.publish_outbound,
}
DOWNLOAD_HANDLERS = {
    NETWORK_TWITTER: twitter_api.download_media,
    NETWORK_TELEGRAM: telegram_api.download_media,
    NETWORK_MASTODON: mastodon_api.download_media,
    NETWORK_THREADS: threads_api.download_media,
    NETWORK_BLUESKY: bluesky_api.download_media,
    NETWORK_RSS: rss_api.download_media,
    NETWORK_INSTAGRAM: instagram_api.download_media,
}


def _fetch_since(
    engine: Engine,
    account: Account,
    since: datetime | None,
) -> datetime | None:
    if since is not None:
        return since
    now = datetime.now(timezone.utc)
    last_sync = get_last_synced_at(engine, account.id)
    if last_sync:
        return last_sync - timedelta(hours=WATCH_OVERLAP_HOURS)
    return now - timedelta(hours=WATCH_INITIAL_LOOKBACK_HOURS)


def _group_by_conversation(posts: list[Post]) -> list[list[Post]]:
    groups: dict[str, list[Post]] = defaultdict(list)
    for post in posts:
        groups[post.conversation_id].append(post)
    return [sort_chronologically(group) for group in groups.values()]


def _filter_original_posts(posts: list[Post], mirrored_ids: set[str]) -> list[Post]:
    if not mirrored_ids:
        return posts
    filtered = [p for p in posts if p.id not in mirrored_ids]
    skipped = len(posts) - len(filtered)
    if skipped:
        logger.info("Skipped %d mirrored post(s) created by sync", skipped)
    return filtered


def _destination_accounts(source: Account, all_accounts: list[Account]) -> list[Account]:
    return [
        account
        for account in all_accounts
        if account.id != source.id and account.network not in SOURCE_ONLY_NETWORKS
    ]


async def _download_media_flat(
    engine: Engine, source: Account, posts: list[Post]
) -> list[bytes]:
    download = DOWNLOAD_HANDLERS[source.network]
    return [
        await download(item, engine, source.id)
        for post in posts
        for item in post.media
    ]


def _slice_media_bytes(
    outbounds: list[OutboundPost], all_bytes: list[bytes]
) -> list[list[bytes]]:
    offset = 0
    sliced: list[list[bytes]] = []
    for outbound in outbounds:
        n = len(outbound.media)
        sliced.append(all_bytes[offset : offset + n])
        offset += n
    return sliced


def _dest_id_per_source_post(
    outbounds: list[OutboundPost], dest_ids: list[str]
) -> dict[str, str]:
    """Map each source post id to the last dest id published for it."""
    mapping: dict[str, str] = {}
    for outbound, dest_id in zip(outbounds, dest_ids):
        for source_id in outbound.source_post_ids:
            mapping[source_id] = dest_id
    return mapping


async def _chain_anchor(
    engine: Engine,
    source: Account,
    dest: Account,
    thread: list[Post],
    batch: list[Post],
) -> tuple[str | None, tuple[str, str] | None]:
    """Reply target when this batch continues a partially synced conversation."""
    if not batch:
        return None, None

    ordered = sort_chronologically(thread)
    first_id = batch[0].id
    idx = next((i for i, post in enumerate(ordered) if post.id == first_id), -1)
    if idx <= 0:
        return None, None

    prev_post = ordered[idx - 1]
    if not is_synced(engine, source.id, prev_post.id, dest.id):
        return None, None

    dest_post_id = get_dest_post_id(engine, source.id, prev_post.id, dest.id)
    if not dest_post_id:
        return None, None

    if dest.network == NETWORK_BLUESKY:
        bluesky_reply = await bluesky_api.resolve_reply_target(
            engine, dest.id, dest_post_id
        )
        return None, bluesky_reply

    return dest_post_id, None


async def _publish_batch(
    engine: Engine,
    source: Account,
    dest: Account,
    batch: list[Post],
    thread: list[Post],
    post_delay_seconds: float,
) -> tuple[list[str], list[OutboundPost]]:
    """Publish one batch to a destination account. Returns created post IDs."""
    publish = PUBLISH_HANDLERS[dest.network]
    limits = get_network_limits(dest.network)
    await unwrap_posts_text(batch)

    if source.network == NETWORK_INSTAGRAM and instagram_api.is_story_batch(batch):
        outbounds = instagram_api.build_story_outbounds(batch, limits)
        if len(batch) > 1:
            logger.info(
                "[%s -> %s] Merging %d Instagram story slide(s) into %d post(s)",
                account_display_name(source, engine),
                account_display_name(dest, engine),
                len(batch),
                len(outbounds),
            )
    else:
        outbounds = build_outbound_posts(batch, limits)
    if len(outbounds) > 1:
        logger.info(
            "[%s -> %s] Publishing %d message(s) in order",
            account_display_name(source, engine),
            account_display_name(dest, engine),
            len(outbounds),
        )

    all_bytes = await _download_media_flat(engine, source, batch)
    media_slices = _slice_media_bytes(outbounds, all_bytes)

    reply_to_id, bluesky_reply_to = await _chain_anchor(
        engine, source, dest, thread, batch
    )

    dest_ids: list[str] = []
    for i, (outbound, bytes_chunk) in enumerate(zip(outbounds, media_slices)):
        if dest.network == NETWORK_THREADS:
            dest_id = await threads_api.publish_outbound(
                engine,
                dest.id,
                outbound,
                bytes_chunk,
                reply_to_id=reply_to_id,
            )
            reply_to_id = dest_id
        elif dest.network == NETWORK_BLUESKY:
            dest_id, bluesky_reply_to = await bluesky_api.publish_outbound(
                engine,
                dest.id,
                outbound,
                bytes_chunk,
                reply_to=bluesky_reply_to,
            )
        elif dest.network == NETWORK_TWITTER:
            dest_id = await twitter_api.publish_outbound(
                engine,
                dest.id,
                outbound,
                bytes_chunk,
                reply_to_id=reply_to_id,
            )
            reply_to_id = dest_id
        elif dest.network == NETWORK_MASTODON:
            dest_id = await mastodon_api.publish_outbound(
                engine,
                dest.id,
                outbound,
                bytes_chunk,
                in_reply_to_id=reply_to_id,
            )
            reply_to_id = dest_id
        else:
            dest_id = await publish(engine, dest.id, outbound, bytes_chunk)
        dest_ids.append(dest_id)
        if post_delay_seconds > 0 and i < len(outbounds) - 1:
            await asyncio.sleep(post_delay_seconds)
    return dest_ids, outbounds


def _record_success(
    engine: Engine,
    source: Account,
    dest: Account,
    batch: list[Post],
    outbounds: list[OutboundPost],
    dest_ids: list[str],
) -> int:
    """Record sync mappings and mirrored posts. Returns count of new mappings."""
    if not dest_ids:
        return 0

    per_post_dest = _dest_id_per_source_post(outbounds, dest_ids)
    recorded = 0
    for post in batch:
        dest_post_id = per_post_dest.get(post.id, dest_ids[-1])
        if mark_synced(
            engine,
            source_account_id=source.id,
            source_post_id=post.id,
            dest_account_id=dest.id,
            dest_post_id=dest_post_id,
        ):
            recorded += 1

    for dest_id in dest_ids:
        record_mirrored_post(engine, dest.id, dest_id)

    return recorded


async def sync_account(
    engine: Engine,
    source: Account,
    all_accounts: list[Account],
    since: datetime | None = None,
    enforce_min_age: bool = True,
    min_age_minutes: int = POST_MIN_AGE_MINUTES,
    post_delay_seconds: float = 0,
) -> int:
    dest_accounts = _destination_accounts(source, all_accounts)
    if not dest_accounts:
        logger.info(
            "[%s] No publishable accounts configured — nothing to sync",
            account_display_name(source, engine),
        )
        return 0

    fetch_since = _fetch_since(engine, source, since)
    max_pages = (
        None
        if since is not None or source.network in (NETWORK_RSS, NETWORK_INSTAGRAM)
        else WATCH_MAX_PAGES
    )
    source_name = account_display_name(source, engine)

    if max_pages and source.network not in (NETWORK_RSS, NETWORK_INSTAGRAM):
        logger.info(
            "[%s] Fetching posts since %s (max %d API page(s))",
            source_name,
            fetch_since.strftime("%Y-%m-%d %H:%M UTC") if fetch_since else "all",
            max_pages,
        )
    elif since is not None:
        logger.info(
            "[%s] Backfill: fetching posts since %s",
            source_name,
            fetch_since.strftime("%Y-%m-%d %H:%M UTC") if fetch_since else "all",
        )
    else:
        logger.info(
            "[%s] Fetching posts since %s",
            source_name,
            fetch_since.strftime("%Y-%m-%d %H:%M UTC") if fetch_since else "all",
        )

    try:
        posts = await FETCH_HANDLERS[source.network](
            engine,
            source.id,
            since=fetch_since,
            include_replies=True,
            max_pages=max_pages,
        )
    except Exception:
        logger.exception(
            "[%s] Fetch failed — skipping until next cycle",
            source_name,
        )
        return 0
    mirrored_ids = get_mirrored_post_ids(engine, source.id)
    posts = _filter_original_posts(posts, mirrored_ids)

    pending = sum(
        1
        for p in posts
        if any(
            not is_synced(engine, source.id, p.id, dest.id)
            for dest in dest_accounts
        )
    )
    logger.info(
        "[%s] Eligible: %d post(s), pending sync to at least one account: %d",
        source_name,
        len(posts),
        pending,
    )

    if pending == 0:
        set_last_synced_at(engine, source.id, datetime.now(timezone.utc))
        return 0

    synced = 0

    for thread in _group_by_conversation(posts):
        for dest in dest_accounts:
            dest_name = account_display_name(dest, engine)
            batch = collect_ready_batch(
                thread,
                is_synced=lambda pid, d=dest: is_synced(
                    engine, source.id, pid, d.id
                ),
                enforce_min_age=enforce_min_age,
                min_age_minutes=min_age_minutes,
            )
            if not batch:
                continue

            post_ids = ", ".join(p.id for p in batch)
            try:
                dest_ids, outbounds = await _publish_batch(
                    engine, source, dest, batch, thread, post_delay_seconds
                )
            except Exception:
                logger.exception(
                    "[%s -> %s] Publish failed for post(s) %s — will retry later",
                    source_name,
                    dest_name,
                    post_ids,
                )
                if post_delay_seconds > 0:
                    await asyncio.sleep(post_delay_seconds)
                continue

            recorded = _record_success(
                engine, source, dest, batch, outbounds, dest_ids
            )
            synced += recorded
            logger.info(
                "[%s -> %s] Synced post(s) %s -> %s",
                source_name,
                dest_name,
                post_ids,
                ", ".join(dest_ids),
            )

            if post_delay_seconds > 0:
                await asyncio.sleep(post_delay_seconds)

    set_last_synced_at(engine, source.id, datetime.now(timezone.utc))
    return synced


async def run_sync(
    engine: Engine,
    since: datetime | None = None,
    enforce_min_age: bool = True,
) -> int:
    accounts = list_accounts(engine)
    if not accounts:
        logger.warning(
            "No accounts configured. Run --auth for at least one network."
        )
        return 0

    post_delay = 0 if enforce_min_age else BACKFILL_POST_DELAY_SECONDS
    if post_delay:
        logger.info("Backfill mode: %.1fs delay between posts", post_delay)

    total = 0
    for source in accounts:
        source_name = account_display_name(source, engine)
        try:
            total += await sync_account(
                engine,
                source,
                accounts,
                since=since,
                enforce_min_age=enforce_min_age,
                post_delay_seconds=post_delay,
            )
        except Exception:
            logger.exception(
                "[%s] Sync failed — continuing with other accounts",
                source_name,
            )
    return total
