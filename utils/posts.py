from datetime import timezone

from apis.types import Post


def sort_chronologically(posts: list[Post]) -> list[Post]:
    def chronological_key(post: Post):
        try:
            identity = (0, int(post.id))
        except ValueError:
            identity = (1, post.id)
        return post.created_at.astimezone(timezone.utc), identity

    return sorted(posts, key=chronological_key)
