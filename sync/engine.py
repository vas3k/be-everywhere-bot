import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy.engine import Engine

import apis.mastodon as mastodon_api
import apis.telegram as telegram_api
import apis.twitter as twitter_api
from apis.types import OutboundPost, Post
from apis.urls import unwrap_posts_text
from config import (
    BACKFILL_POST_DELAY_SECONDS,
    NETWORK_MASTODON,
    NETWORK_TELEGRAM,
    NETWORK_TWITTER,
    POST_MIN_AGE_MINUTES,
    SYNC_PAIRS,
    WATCH_INITIAL_LOOKBACK_HOURS,
    WATCH_MAX_PAGES,
    WATCH_OVERLAP_HOURS,
)
from db.posts import get_last_synced_at, is_posted, mark_posted, set_last_synced_at
from sync.thread_processor import (
    build_outbound_posts,
    collect_ready_batch,
    get_destination_limits,
)

logger = logging.getLogger(__name__)

FETCH_HANDLERS = {NETWORK_TWITTER: twitter_api.fetch_posts}
PUBLISH_HANDLERS = {
    NETWORK_TELEGRAM: telegram_api.publish_outbound,
    NETWORK_MASTODON: mastodon_api.publish_outbound,
}
DOWNLOAD_HANDLERS = {NETWORK_TWITTER: twitter_api.download_media}


def _fetch_since(
    engine: Engine,
    source: str,
    destination: str,
    since: datetime | None,
) -> datetime | None:
    """Narrow the timeline window to cut owned-read costs."""
    if since is not None:
        return since
    now = datetime.now(timezone.utc)
    last_sync = get_last_synced_at(engine, source, destination)
    if last_sync:
        return last_sync - timedelta(hours=WATCH_OVERLAP_HOURS)
    return now - timedelta(hours=WATCH_INITIAL_LOOKBACK_HOURS)


def _group_by_conversation(posts: list[Post]) -> list[list[Post]]:
    groups: dict[str, list[Post]] = defaultdict(list)
    for post in posts:
        groups[post.conversation_id].append(post)
    return [sorted(group, key=lambda p: p.created_at) for group in groups.values()]


async def _download_media_flat(
    engine: Engine, source_network: str, posts: list[Post]
) -> list[bytes]:
    if source_network not in DOWNLOAD_HANDLERS:
        return []
    download = DOWNLOAD_HANDLERS[source_network]
    token = ""
    if source_network == NETWORK_TWITTER:
        token = twitter_api.get_bearer_token(engine)
    return [
        await download(item, token)
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


async def _publish_batch(
    engine: Engine,
    source: str,
    destination: str,
    batch: list[Post],
    limits,
    post_delay_seconds: float,
) -> str | None:
    """Publish one batch. Returns first destination message id, or None on failure."""
    publish = PUBLISH_HANDLERS[destination]
    await unwrap_posts_text(batch)

    outbounds = build_outbound_posts(batch, limits)
    all_bytes = await _download_media_flat(engine, source, batch)
    media_slices = _slice_media_bytes(outbounds, all_bytes)

    first_dest_id = ""
    for i, (outbound, bytes_chunk) in enumerate(zip(outbounds, media_slices)):
        dest_id = await publish(engine, outbound, bytes_chunk)
        if not first_dest_id:
            first_dest_id = dest_id
        if post_delay_seconds > 0 and i < len(outbounds) - 1:
            await asyncio.sleep(post_delay_seconds)

    return first_dest_id or None


async def sync_pair(
    engine: Engine,
    source: str,
    destination: str,
    since: datetime | None = None,
    enforce_min_age: bool = True,
    min_age_minutes: int = POST_MIN_AGE_MINUTES,
    post_delay_seconds: float = 0,
) -> int:
    fetch_since = _fetch_since(engine, source, destination, since)
    max_pages = None if since is not None else WATCH_MAX_PAGES
    if max_pages:
        logger.info(
            "[%s -> %s] Fetching posts since %s (max %d API page(s))",
            source,
            destination,
            fetch_since.strftime("%Y-%m-%d %H:%M UTC"),
            max_pages,
        )
    else:
        logger.info(
            "[%s -> %s] Backfill: fetching posts since %s",
            source,
            destination,
            fetch_since.strftime("%Y-%m-%d %H:%M UTC"),
        )
    posts = await FETCH_HANDLERS[source](
        engine,
        since=fetch_since,
        include_replies=True,
        max_pages=max_pages,
    )
    to_sync = sum(
        1 for p in posts if not is_posted(engine, source, p.id, destination)
    )
    logger.info(
        "[%s -> %s] Eligible: %d post(s), already synced: %d, to sync: %d",
        source,
        destination,
        len(posts),
        len(posts) - to_sync,
        to_sync,
    )

    if to_sync == 0:
        return 0

    limits = get_destination_limits(destination)
    synced = 0

    for thread in _group_by_conversation(posts):
        batch = collect_ready_batch(
            thread,
            is_posted=lambda pid: is_posted(engine, source, pid, destination),
            enforce_min_age=enforce_min_age,
            min_age_minutes=min_age_minutes,
        )
        if not batch:
            continue

        post_ids = ", ".join(p.id for p in batch)
        try:
            dest_id = await _publish_batch(
                engine, source, destination, batch, limits, post_delay_seconds
            )
        except Exception:
            logger.exception(
                "[%s -> %s] Publish failed for post(s) %s — left unsynced, will retry later",
                source,
                destination,
                post_ids,
            )
            if post_delay_seconds > 0:
                await asyncio.sleep(post_delay_seconds)
            continue

        if not dest_id:
            logger.error(
                "[%s -> %s] No destination id for post(s) %s — left unsynced",
                source,
                destination,
                post_ids,
            )
            continue

        for post in batch:
            if mark_posted(
                engine,
                source_network=source,
                source_post_id=post.id,
                destination_network=destination,
                destination_post_id=dest_id,
            ):
                synced += 1
                logger.info(
                    "Synced %s:%s -> %s:%s", source, post.id, destination, dest_id
                )

        if post_delay_seconds > 0:
            await asyncio.sleep(post_delay_seconds)

    set_last_synced_at(engine, source, destination, datetime.now(timezone.utc))
    return synced


async def run_sync(
    engine: Engine,
    since: datetime | None = None,
    enforce_min_age: bool = True,
) -> int:
    post_delay = 0 if enforce_min_age else BACKFILL_POST_DELAY_SECONDS
    if post_delay:
        logger.info("Backfill mode: %.1fs delay between posts", post_delay)

    total = 0
    for source, destination in SYNC_PAIRS:
        total += await sync_pair(
            engine,
            source,
            destination,
            since=since,
            enforce_min_age=enforce_min_age,
            post_delay_seconds=post_delay,
        )
    return total
