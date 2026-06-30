import logging

from apis.types import Post

logger = logging.getLogger(__name__)


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
