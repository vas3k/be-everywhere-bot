from datetime import datetime, timezone

from apis.types import Post


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
