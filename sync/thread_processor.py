"""Group source posts into destination-sized outbound posts."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from apis.types import MediaItem, OutboundPost, Post
from config import NETWORK_LIMITS, NetworkLimits, TELEGRAM_LIMITS


def is_old_enough(post: Post, min_age_minutes: int) -> bool:
    age = datetime.now(timezone.utc) - post.created_at.astimezone(timezone.utc)
    return age >= timedelta(minutes=min_age_minutes)


def collect_ready_batch(
    thread: list[Post],
    *,
    is_synced: Callable[[str], bool],
    enforce_min_age: bool,
    min_age_minutes: int,
) -> list[Post]:
    """Next unsynced source posts in thread order; stops at first not old enough."""
    batch: list[Post] = []
    for post in thread:
        if is_synced(post.id):
            continue
        if enforce_min_age and not is_old_enough(post, min_age_minutes):
            break
        batch.append(post)
    return batch


def split_text(text: str, max_len: int) -> list[str]:
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text.strip()
    separators = ["\n\n", "\n", ". ", "! ", "? ", ", ", " "]

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        window = remaining[:max_len]
        split_at = 0
        for sep in separators:
            idx = window.rfind(sep)
            if idx > 0:
                split_at = idx + len(sep)
                break
        if split_at <= 0:
            split_at = max_len

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return [c for c in chunks if c]


def _chunk(items: list[MediaItem], size: int) -> list[list[MediaItem]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def build_outbound_posts(
    posts: list[Post],
    limits: NetworkLimits | None = None,
) -> list[OutboundPost]:
    """Combine thread posts into one or more outbound messages."""
    if not posts:
        return []

    limits = limits or TELEGRAM_LIMITS
    source_ids = [p.id for p in posts]
    combined = "\n\n".join(p.text for p in posts if p.text).strip()
    media = [m for p in posts for m in p.media]

    if not media:
        if not combined:
            return [OutboundPost(text="", source_post_ids=source_ids)]
        return [
            OutboundPost(text=chunk, source_post_ids=source_ids)
            for chunk in split_text(combined, limits.max_text)
        ]

    out: list[OutboundPost] = []
    text_remaining = combined

    for batch in _chunk(media, limits.max_media_group):
        caption = ""
        if text_remaining:
            if len(text_remaining) <= limits.max_caption:
                caption, text_remaining = text_remaining, ""
            else:
                parts = split_text(text_remaining, limits.max_caption)
                caption = parts[0]
                text_remaining = "\n\n".join(parts[1:]).strip() if len(parts) > 1 else ""
        out.append(
            OutboundPost(text=caption, media=batch, source_post_ids=source_ids)
        )

    for chunk in split_text(text_remaining, limits.max_text):
        out.append(OutboundPost(text=chunk, source_post_ids=source_ids))

    return out


def get_network_limits(network: str) -> NetworkLimits:
    if network not in NETWORK_LIMITS:
        raise ValueError(f"No limits configured for network: {network}")
    return NETWORK_LIMITS[network]
