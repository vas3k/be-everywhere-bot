from datetime import datetime, timezone

from apis.threads import (
    _extract_media,
    _item_to_post,
    _public_https_url,
    _skip_reason,
    _strip_trailing_links,
    _threads_media_type,
    filter_originals_and_threads,
)
from apis.types import Post


def test_public_https_url():
    assert _public_https_url("https://cdn.example/a.jpg") is True
    assert _public_https_url("tgfile:abc") is False
    assert _public_https_url("") is False


def test_threads_media_type():
    assert _threads_media_type("IMAGE") == "photo"
    assert _threads_media_type("VIDEO") == "video"
    assert _threads_media_type("GIF") == "video"


def test_extract_media_carousel():
    item = {
        "media_type": "CAROUSEL_ALBUM",
        "children": {
            "data": [
                {"media_type": "IMAGE", "media_url": "https://cdn/1.jpg"},
                {"media_type": "VIDEO", "media_url": "https://cdn/2.mp4"},
            ]
        },
    }
    media = _extract_media(item)
    assert len(media) == 2


def test_strip_trailing_threads_url():
    text = "Check this https://www.threads.net/@user/post/abc"
    assert _strip_trailing_links(text, has_media=True) == "Check this"


def test_skip_reason_quote_repost_at_reply():
    assert _skip_reason({"is_quote_post": True}) == "quote"
    assert _skip_reason({"reposted_post": {"id": "1"}}) == "repost"
    assert _skip_reason({"media_type": "REPOST_FACADE"}) == "repost"
    assert _skip_reason({"text": "@user hello"}) == "at_reply"


def test_item_to_post_reply_thread():
    item = {
        "id": "200",
        "text": "second",
        "timestamp": "2026-06-30T12:00:00+0000",
        "media_type": "TEXT_POST",
        "is_reply": True,
        "root_post": {"id": "100"},
        "replied_to": {"id": "150"},
    }
    post = _item_to_post(item, "user-1")
    assert post.conversation_id == "100"
    assert post.in_reply_to_id == "150"
    assert post.is_thread_root is False


def test_filter_originals_drops_foreign_replies():
    author = "user-1"
    root = Post(
        id="1",
        text="root",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="1",
        author_id=author,
        is_thread_root=True,
    )
    own = Post(
        id="2",
        text="mine",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="1",
        author_id=author,
        in_reply_to_id="1",
    )
    foreign = Post(
        id="3",
        text="other",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="9",
        author_id=author,
        in_reply_to_id="999",
    )
    kept = filter_originals_and_threads([root, own, foreign])
    assert [p.id for p in kept] == ["1", "2"]
