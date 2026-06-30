import logging

from apis.types import Post
from config import SOURCE_ONLY_MARKER

logger = logging.getLogger(__name__)


def is_source_only_post(post: Post, marker: str = SOURCE_ONLY_MARKER) -> bool:
    """True when post text ends with the source-only marker (e.g. /x)."""
    return bool(post.text and post.text.rstrip().endswith(marker))


def exclude_source_only_posts(
    posts: list[Post],
    marker: str = SOURCE_ONLY_MARKER,
) -> list[Post]:
    """Drop posts marked source-only; they stay on the origin network."""
    if not posts:
        return []
    kept = [p for p in posts if not is_source_only_post(p, marker)]
    skipped = len(posts) - len(kept)
    if skipped:
        logger.info(
            "Skipped %d source-only post(s) marked with %r",
            skipped,
            marker,
        )
    return kept


def filter_own_threads(posts: list[Post]) -> list[Post]:
    """Drop replies to other people; keep own threads and root posts."""
    if not posts:
        return []

    author_id = posts[0].author_id
    own_ids = {p.id for p in posts}
    own_thread_roots = {p.id for p in posts if p.conversation_id == p.id}

    kept: list[Post] = []
    skipped = 0

    for post in posts:
        if post.in_reply_to_user_id is not None:
            if str(post.in_reply_to_user_id) != str(author_id):
                skipped += 1
                continue
        elif post.in_reply_to_id is not None:
            in_own_thread = (
                post.in_reply_to_id in own_ids
                or post.conversation_id in own_thread_roots
            )
            if not in_own_thread:
                skipped += 1
                continue
        kept.append(post)

    if skipped:
        logger.info("Filtered out %d reply/replies to other people", skipped)
    return kept
