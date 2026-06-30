"""Group source posts into destination-sized outbound posts."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from apis.types import MediaItem, OutboundPost, Post
from sync.posts import sort_chronologically
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
    for post in sort_chronologically(thread):
        if is_synced(post.id):
            continue
        if enforce_min_age and not is_old_enough(post, min_age_minutes):
            break
        batch.append(post)
    return batch


LINE_SEPARATORS = ("\n\n", "\n")
SENTENCE_SEPARATORS = (". ", "! ", "? ")


def _split_at_separator(window: str, separators: tuple[str, ...]) -> int:
    for sep in separators:
        idx = window.rfind(sep)
        if idx > 0:
            return idx + len(sep)
    return 0


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
        split_at = _split_at_separator(window, LINE_SEPARATORS)
        if split_at <= 0:
            split_at = _split_at_separator(window, SENTENCE_SEPARATORS)
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


def _chunk(items: list[MediaItem], size: int) -> list[list[MediaItem]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _outbound_for_single_post(post: Post, limits: NetworkLimits) -> list[OutboundPost]:
    source_ids = [post.id]
    text = (post.text or "").strip()
    media = list(post.media)

    if not media:
        if not text:
            return []
        return [
            OutboundPost(text=chunk, source_post_ids=source_ids)
            for chunk in split_text(text, limits.max_text)
        ]

    if not limits.allows_mixed_media:
        photos = [item for item in media if item.media_type == "photo"]
        videos = [
            item for item in media if item.media_type in ("video", "animated_gif")
        ]
        if photos and videos:
            out: list[OutboundPost] = []
            photo_post = Post(
                id=post.id,
                text=post.text,
                created_at=post.created_at,
                conversation_id=post.conversation_id,
                author_id=post.author_id,
                media=photos,
                in_reply_to_id=post.in_reply_to_id,
                in_reply_to_user_id=post.in_reply_to_user_id,
                is_thread_root=post.is_thread_root,
            )
            video_post = Post(
                id=post.id,
                text="",
                created_at=post.created_at,
                conversation_id=post.conversation_id,
                author_id=post.author_id,
                media=videos,
                in_reply_to_id=post.in_reply_to_id,
                in_reply_to_user_id=post.in_reply_to_user_id,
                is_thread_root=post.is_thread_root,
            )
            out.extend(_outbound_for_single_post(photo_post, limits))
            out.extend(_outbound_for_single_post(video_post, limits))
            return out

    out: list[OutboundPost] = []
    text_remaining = text

    for batch in _chunk(media, limits.max_media_group):
        caption = ""
        if text_remaining:
            if len(text_remaining) <= limits.max_caption:
                caption, text_remaining = text_remaining, ""
            else:
                parts = split_text(text_remaining, limits.max_caption)
                caption = parts[0]
                text_remaining = (
                    "\n\n".join(parts[1:]).strip() if len(parts) > 1 else ""
                )
        out.append(
            OutboundPost(text=caption, media=batch, source_post_ids=source_ids)
        )

    for chunk in split_text(text_remaining, limits.max_text):
        out.append(OutboundPost(text=chunk, source_post_ids=source_ids))

    return out


def build_outbound_posts(
    posts: list[Post],
    limits: NetworkLimits | None = None,
) -> list[OutboundPost]:
    """Split source posts into ordered outbound messages sized for destination limits."""
    if not posts:
        return []

    limits = limits or TELEGRAM_LIMITS
    out: list[OutboundPost] = []
    for post in sort_chronologically(posts):
        out.extend(_outbound_for_single_post(post, limits))
    return out


def get_network_limits(network: str) -> NetworkLimits:
    if network not in NETWORK_LIMITS:
        raise ValueError(f"No limits configured for network: {network}")
    return NETWORK_LIMITS[network]
