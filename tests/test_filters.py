from datetime import datetime, timezone

from sync.filters import filter_own_threads
from apis.types import Post


def test_filter_own_threads_keeps_own_replies():
    author = "42"
    root = Post(
        id="1",
        text="root",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="1",
        author_id=author,
        is_thread_root=True,
    )
    own_reply = Post(
        id="2",
        text="mine",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="1",
        author_id=author,
        in_reply_to_id="1",
        in_reply_to_user_id=author,
    )
    foreign_by_user = Post(
        id="3",
        text="other",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="9",
        author_id=author,
        in_reply_to_id="9",
        in_reply_to_user_id="999",
    )
    foreign_by_thread = Post(
        id="4",
        text="other2",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="9",
        author_id=author,
        in_reply_to_id="999",
    )
    kept = filter_own_threads([root, own_reply, foreign_by_user, foreign_by_thread])
    assert [p.id for p in kept] == ["1", "2"]
