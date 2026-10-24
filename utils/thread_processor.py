"""Group source posts into destination-sized outbound posts."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from apis.types import OutboundPost, Post
from config import NETWORK_LIMITS, NetworkLimits, TELEGRAM_LIMITS
from utils.posts import sort_chronologically


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
    """Next unsynced source posts in thread order.

    Min-age is enforced only on the thread's main (starting) post. Replies are
    included as soon as the root is ready or already synced — they may still be
    in progress on the source network, but the thread processor posts them with
    the root rather than waiting for each reply to age independently.
    """
    ordered = sort_chronologically(thread)
    root = next((post for post in ordered if post.is_thread_root), ordered[0] if ordered else None)
    batch: list[Post] = []
    for post in ordered:
        if is_synced(post.id):
            continue
        if (
            enforce_min_age
            and root is not None
            and post.id == root.id
            and not is_old_enough(post, min_age_minutes)
        ):
            break
        batch.append(post)
    return batch


LINE_SEPARATORS = ("\n\n", "\n")
SENTENCE_SEPARATORS = (". ", "! ", "? ")


def split_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks <= max_len.

    Break priority: paragraph/line (\\n\\n, \\n), then sentence (. ), then word (space).
    Never splits mid-word unless a single word exceeds max_len.
    """
    if not text:
        return []
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text.strip()

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        window = remaining[:max_len]
        split_at = 0
        for separator in (*LINE_SEPARATORS, *SENTENCE_SEPARATORS):
            index = window.rfind(separator)
            if index > 0:
                split_at = index + len(separator)
                break
        if split_at <= 0:
            idx = window.rfind(" ")
            if idx > 0:
                split_at = idx + 1
            else:
                split_at = max_len

        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    return chunks


def build_outbound_posts(
    posts: list[Post], limits: NetworkLimits | None = None,
) -> list[OutboundPost]:
    if not posts:
        return []
    limits = limits or TELEGRAM_LIMITS
    if min(limits.max_text, limits.max_caption, limits.max_media_group) <= 0:
        raise ValueError("Destination limits must be positive")

    ordered = sort_chronologically(posts)
    source_ids = [post.id for post in ordered]
    remaining = "\n\n".join(post.text.strip() for post in ordered if post.text.strip())
    media = [item for post in ordered for item in post.media]
    groups = [media]
    if not limits.allows_mixed_media:
        groups = [
            [item for item in media if item.media_type == "photo"],
            [item for item in media if item.media_type != "photo"],
        ]

    outbounds = []
    for group in groups:
        group_size = limits.max_media_group
        if group and group[0].media_type != "photo" and limits.max_videos_per_post is not None:
            group_size = min(group_size, limits.max_videos_per_post)
        for offset in range(0, len(group), group_size):
            attachments = group[offset:offset + group_size]
            caption = split_text(remaining, limits.max_caption)[0] if remaining else ""
            remaining = remaining[len(caption):].lstrip()
            outbounds.append(OutboundPost(caption, attachments, source_ids))
    for text in split_text(remaining, limits.max_text):
        outbounds.append(OutboundPost(text=text, source_post_ids=source_ids))
    return outbounds


def get_network_limits(network: str) -> NetworkLimits:
    if network not in NETWORK_LIMITS:
        raise ValueError(f"No limits configured for network: {network}")
    return NETWORK_LIMITS[network]
