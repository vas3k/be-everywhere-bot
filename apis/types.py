from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class MediaItem:
    url: str
    media_type: str  # "photo" | "video" | "animated_gif"
    alt_text: str | None = None


@dataclass
class Post:
    """A single item from a source network (e.g. one tweet or toot)."""

    id: str
    text: str
    created_at: datetime
    conversation_id: str
    author_id: str
    media: list[MediaItem] = field(default_factory=list)
    in_reply_to_id: str | None = None
    in_reply_to_user_id: str | None = None
    is_thread_root: bool = False


@dataclass
class OutboundPost:
    """One message to publish on a destination network."""

    text: str
    media: list[MediaItem] = field(default_factory=list)
    source_post_ids: list[str] = field(default_factory=list)


def _chronological_key(post: Post) -> tuple[datetime, int | str]:
    """Oldest first; tie-break with post id (numeric or lexicographic, e.g. Bluesky TID)."""
    created = post.created_at.astimezone(timezone.utc)
    try:
        tiebreaker: int | str = int(post.id)
    except ValueError:
        tiebreaker = post.id
    return (created, tiebreaker)


def sort_chronologically(posts: list[Post]) -> list[Post]:
    return sorted(posts, key=_chronological_key)
