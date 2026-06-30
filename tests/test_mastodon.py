from datetime import datetime, timezone

import httpx
import pytest
import respx

from apis.mastodon import (
    _extract_media,
    _is_photo,
    _is_video,
    _media_content_type,
    _media_filename,
    _normalize_thread_roots,
    _partition_media_for_publish,
    _status_to_post,
    _strip_html,
    publish_outbound,
)
from apis.types import MediaItem, OutboundPost, Post
from config import NETWORK_MASTODON
from db.accounts import create_account, set_credentials


def _items(*types: str) -> tuple[list[MediaItem], list[bytes]]:
    media = [
        MediaItem(url=f"https://example.com/{i}", media_type=kind)
        for i, kind in enumerate(types)
    ]
    raw = [f"{kind}-{i}".encode() for i, kind in enumerate(types)]
    return media, raw


def test_strip_html():
    html = "<p>Hello<br/>world</p>"
    assert "Hello" in _strip_html(html)
    assert "world" in _strip_html(html)


def test_extract_media_types():
    status = {
        "media_attachments": [
            {"type": "image", "url": "https://m.test/a.png", "description": "pic"},
            {"type": "gifv", "url": "https://m.test/b.mp4"},
        ]
    }
    items = _extract_media(status)
    assert items[0].media_type == "photo"
    assert items[0].alt_text == "pic"
    assert items[1].media_type == "animated_gif"


def test_status_to_post_skips_reblog():
    author = "10"
    assert _status_to_post({"reblog": {}}, author) is None


def test_status_to_post_foreign_reply_parsed():
    author = "10"
    foreign = {
        "id": "1",
        "content": "reply",
        "created_at": "2026-06-30T10:00:00Z",
        "in_reply_to_account_id": "99",
        "in_reply_to_id": "50",
    }
    post = _status_to_post(foreign, author)
    assert post is not None
    assert post.in_reply_to_user_id == "99"


def test_status_to_post_own_reply_thread():
    author = "10"
    status = {
        "id": "2",
        "content": "<p>follow-up</p>",
        "created_at": "2026-06-30T11:00:00Z",
        "in_reply_to_id": "1",
        "in_reply_to_account_id": "10",
        "media_attachments": [],
    }
    post = _status_to_post(status, author)
    assert post is not None
    assert post.conversation_id == "1"
    assert post.text == "follow-up"


def test_normalize_thread_roots(post_factory, utc_now):
    root = post_factory(
        "1",
        created_at=utc_now,
        conversation_id="1",
        in_reply_to_id=None,
    )
    reply = post_factory(
        "2",
        created_at=utc_now,
        conversation_id="2",
        in_reply_to_id="1",
    )
    normalized = _normalize_thread_roots([root, reply])
    assert all(p.conversation_id == "1" for p in normalized)
    assert normalized[0].is_thread_root is True
    assert normalized[1].is_thread_root is False


def test_partition_photos_only():
    media, raw = _items("photo", "photo", "photo")
    photos, videos = _partition_media_for_publish(media, raw, "post-1")
    assert len(photos) == 3
    assert videos == []


def test_partition_caps_photos_at_four():
    media, raw = _items("photo", "photo", "photo", "photo", "photo")
    photos, videos = _partition_media_for_publish(media, raw, "post-1")
    assert len(photos) == 4
    assert videos == []


def test_partition_video_only():
    media, raw = _items("video", "video")
    photos, videos = _partition_media_for_publish(media, raw, "post-1")
    assert photos == []
    assert len(videos) == 1


def test_partition_mixed_keeps_both_lists_separate():
    media, raw = _items("photo", "photo", "video")
    photos, videos = _partition_media_for_publish(media, raw, "post-1")
    assert len(photos) == 2
    assert len(videos) == 1


def test_is_photo_and_video_helpers():
    assert _is_photo(MediaItem(url="x", media_type="photo"))
    assert _is_video(MediaItem(url="x", media_type="video"))
    assert _is_video(MediaItem(url="x", media_type="animated_gif"))


def test_media_filename_and_content_type():
    assert _media_filename(MediaItem(url="x", media_type="photo"), 0) == "photo0.jpg"
    assert _media_content_type(MediaItem(url="x", media_type="photo")) == "image/jpeg"
    assert _media_content_type(MediaItem(url="x", media_type="video")) == "video/mp4"


@respx.mock
@pytest.mark.asyncio
async def test_publish_outbound_photos_only(engine):
    account = create_account(engine, NETWORK_MASTODON, "default", "acct-1")
    set_credentials(
        engine,
        account.id,
        {
            "instance_url": "https://mastodon.test",
            "access_token": "token",
            "username": "user",
            "account_id": "1",
        },
    )

    media_counter = {"n": 0}

    def next_media_id(_request: httpx.Request) -> httpx.Response:
        media_counter["n"] += 1
        return httpx.Response(200, json={"id": str(media_counter["n"])})

    respx.post("https://mastodon.test/api/v1/media").mock(side_effect=next_media_id)

    status_calls: list[str] = []

    def record_status(request: httpx.Request) -> httpx.Response:
        status_calls.append(request.read().decode())
        return httpx.Response(200, json={"id": "100"})

    respx.post("https://mastodon.test/api/v1/statuses").mock(side_effect=record_status)

    outbound = OutboundPost(
        text="caption",
        media=[
            MediaItem(url="a", media_type="photo"),
            MediaItem(url="b", media_type="photo"),
        ],
        source_post_ids=["src-1"],
    )
    raw = [b"img1", b"img2"]

    result = await publish_outbound(engine, account.id, outbound, raw)

    assert result.post_id == "100"
    assert media_counter["n"] == 2
    assert len(status_calls) == 1


@respx.mock
@pytest.mark.asyncio
async def test_publish_mixed_outbound_posts_photos_only(engine):
    account = create_account(engine, NETWORK_MASTODON, "default", "acct-1")
    set_credentials(
        engine,
        account.id,
        {
            "instance_url": "https://mastodon.test",
            "access_token": "token",
            "username": "user",
            "account_id": "1",
        },
    )

    respx.post("https://mastodon.test/api/v1/media").mock(
        side_effect=lambda _r: httpx.Response(200, json={"id": "1"})
    )
    status_calls: list[str] = []

    def record_status(request: httpx.Request) -> httpx.Response:
        status_calls.append(request.read().decode())
        return httpx.Response(200, json={"id": "100"})

    respx.post("https://mastodon.test/api/v1/statuses").mock(side_effect=record_status)

    outbound = OutboundPost(
        text="caption",
        media=[
            MediaItem(url="a", media_type="photo"),
            MediaItem(url="b", media_type="photo"),
            MediaItem(url="c", media_type="video"),
        ],
        source_post_ids=["src-1"],
    )
    raw = [b"img1", b"img2", b"vid"]

    result = await publish_outbound(engine, account.id, outbound, raw)

    assert result.post_id == "100"
    assert len(status_calls) == 1
    assert "in_reply_to_id" not in status_calls[0]
