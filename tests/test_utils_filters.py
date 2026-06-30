from datetime import datetime, timezone

from utils.filters import exclude_source_only_posts, filter_own_threads, is_source_only_post
from apis.types import Post


def test_is_source_only_post_only_at_end_ignores_trailing_spaces(post_factory):
    assert is_source_only_post(post_factory("1", text="Stay here /x")) is True
    assert is_source_only_post(post_factory("2", text="Stay here /x  \n")) is True
    assert is_source_only_post(post_factory("3", text="Visit https://example.com/x today")) is False
    assert is_source_only_post(post_factory("4", text="/x is not a suffix here")) is False
    assert is_source_only_post(post_factory("5", text="part /x middle")) is False
    assert is_source_only_post(post_factory("6", text="line one\n/x on second line")) is False


def test_is_source_only_post_detects_trailing_marker(post_factory):
    assert is_source_only_post(post_factory("1", text="Stay here /x")) is True
    assert is_source_only_post(post_factory("2", text="Stay here /x  ")) is True
    assert is_source_only_post(post_factory("3", text="Sync me")) is False
    assert is_source_only_post(post_factory("4", text="/x at start")) is False
    assert is_source_only_post(post_factory("5", text="")) is False


def test_exclude_source_only_posts(post_factory):
    sync = post_factory("1", text="public")
    local = post_factory("2", text="private /x")
    kept = exclude_source_only_posts([sync, local])
    assert [p.id for p in kept] == ["1"]


def test_exclude_source_only_posts_empty():
    assert exclude_source_only_posts([]) == []


def test_filter_own_threads_empty():
    assert filter_own_threads([]) == []


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
