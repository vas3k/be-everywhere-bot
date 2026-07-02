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


def _has_connected_parent(
    post: Post,
    by_id: dict[str, Post],
    cache: dict[str, bool],
) -> bool:
    """True when every ancestor in the reply chain is present (root included)."""
    if post.id in cache:
        return cache[post.id]
    if not post.in_reply_to_id:
        result = True
    elif post.in_reply_to_id not in by_id:
        result = False
    else:
        result = _has_connected_parent(by_id[post.in_reply_to_id], by_id, cache)
    cache[post.id] = result
    return result


def exclude_orphan_thread_replies(posts: list[Post]) -> list[Post]:
    """Drop replies whose parent chain is broken (e.g. root skipped as quote/retweet)."""
    if not posts:
        return []

    by_id = {post.id: post for post in posts}
    cache: dict[str, bool] = {}
    kept: list[Post] = []
    skipped = 0

    for post in posts:
        if _has_connected_parent(post, by_id, cache):
            kept.append(post)
        else:
            skipped += 1

    if skipped:
        logger.info(
            "Filtered out %d orphaned thread reply/replies (missing parent in fetch)",
            skipped,
        )
    return kept


def filter_own_threads(posts: list[Post]) -> list[Post]:
    """Drop replies to other people and orphaned tails of incomplete threads."""
    if not posts:
        return []

    author_id = posts[0].author_id
    kept: list[Post] = []
    skipped = 0

    for post in posts:
        if post.in_reply_to_user_id is not None:
            if str(post.in_reply_to_user_id) != str(author_id):
                skipped += 1
                continue
        kept.append(post)

    if skipped:
        logger.info("Filtered out %d reply/replies to other people", skipped)

    return exclude_orphan_thread_replies(kept)
