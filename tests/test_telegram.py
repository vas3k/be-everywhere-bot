from datetime import datetime, timezone

import httpx
import pytest
import respx

from apis.telegram import (
    TG_FILE_PREFIX,
    _chat_matches,
    _extract_media,
    _file_field,
    _media_filename,
    _media_group_type,
    _merge_album_posts,
    _message_to_post,
    _retry_after_seconds,
    _send_method,
    publish_outbound,
)
from apis.types import MediaItem, OutboundPost
from config import NETWORK_TELEGRAM
from db.accounts import create_account, set_credentials


def test_chat_matches_username():
    chat = {"id": -100123, "username": "mychannel"}
    assert _chat_matches("@mychannel", chat) is True
    assert _chat_matches("@other", chat) is False


def test_chat_matches_numeric_id():
    chat = {"id": -1002403074038, "username": "mychannel"}
    assert _chat_matches("-1002403074038", chat) is True


def test_extract_media_photo_picks_largest():
    message = {
        "photo": [
            {"file_id": "small", "file_size": 100},
            {"file_id": "large", "file_size": 5000},
        ]
    }
    items = _extract_media(message)
    assert len(items) == 1
    assert items[0].url == f"{TG_FILE_PREFIX}large"
    assert items[0].media_type == "photo"


def test_extract_media_video_and_animation():
    video = _extract_media({"video": {"file_id": "vid1"}})
    assert video[0].media_type == "video"
    gif = _extract_media({"animation": {"file_id": "gif1"}})
    assert gif[0].media_type == "animated_gif"


def test_message_to_post_text_only():
    message = {
        "message_id": 42,
        "date": int(datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc).timestamp()),
        "text": "Channel post",
        "chat": {"id": -1001, "username": "news"},
    }
    post = _message_to_post(message, "@news")
    assert post is not None
    assert post.id == "42"
    assert post.text == "Channel post"
    assert post.conversation_id == "42"


def test_message_to_post_media_group():
    message = {
        "message_id": 7,
        "media_group_id": "group-abc",
        "date": int(datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc).timestamp()),
        "caption": "Album",
        "photo": [{"file_id": "p1", "file_size": 100}],
        "chat": {"id": -1001, "username": "news"},
    }
    post = _message_to_post(message, "@news")
    assert post is not None
    assert post.conversation_id == "group-abc"
    assert post.is_thread_root is False


def test_merge_album_posts_combines_slides(post_factory, utc_now):
    slide1 = post_factory(
        "10",
        text="",
        created_at=utc_now,
        conversation_id="group-1",
        media=[MediaItem(url=f"{TG_FILE_PREFIX}a", media_type="photo")],
    )
    slide2 = post_factory(
        "11",
        text="Album caption",
        created_at=utc_now,
        conversation_id="group-1",
        media=[MediaItem(url=f"{TG_FILE_PREFIX}b", media_type="photo")],
    )
    merged = _merge_album_posts([slide2, slide1])
    assert len(merged) == 1
    assert merged[0].id == "10"
    assert merged[0].text == "Album caption"
    assert len(merged[0].media) == 2


def test_media_group_type():
    assert _media_group_type(MediaItem(url="x", media_type="photo")) == "photo"
    assert _media_group_type(MediaItem(url="x", media_type="animated_gif")) == "video"


def test_message_to_post_ignores_other_channel():
    message = {
        "message_id": 1,
        "date": 0,
        "text": "nope",
        "chat": {"id": -1001, "username": "other"},
    }
    assert _message_to_post(message, "@news") is None


def test_send_method_and_file_field():
    assert _send_method(MediaItem(url="x", media_type="photo")) == "sendPhoto"
    assert _send_method(MediaItem(url="x", media_type="animated_gif")) == "sendAnimation"
    assert _send_method(MediaItem(url="x", media_type="video")) == "sendVideo"
    assert _file_field(MediaItem(url="x", media_type="photo")) == "photo"


def test_media_filename():
    assert _media_filename(MediaItem(url="x", media_type="photo"), 0) == "photo0.jpg"
    assert _media_filename(MediaItem(url="x", media_type="video"), 1) == "video1.mp4"


def test_retry_after_seconds_from_parameters():
    response = httpx.Response(
        429,
        json={"description": "Too Many Requests", "parameters": {"retry_after": 12}},
    )
    assert _retry_after_seconds(response) == 12


@respx.mock
@pytest.mark.asyncio
async def test_publish_outbound_send_media_group(engine):
    account = create_account(engine, NETWORK_TELEGRAM, "default", "@channel")
    set_credentials(
        engine,
        account.id,
        {"bot_token": "token", "channel_id": "@channel"},
    )

    captured: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["media"] = request.read()
        return httpx.Response(200, json={"ok": True, "result": [{"message_id": 99}]})

    respx.post("https://api.telegram.org/bottoken/sendMediaGroup").mock(side_effect=capture)

    outbound = OutboundPost(
        text="caption",
        media=[
            MediaItem(url="a", media_type="photo"),
            MediaItem(url="b", media_type="photo"),
        ],
        source_post_ids=["src-1"],
    )
    result = await publish_outbound(engine, account.id, outbound, [b"a", b"b"])
    assert result.post_id == "99"
    assert b"sendMediaGroup" in captured["media"] or b"media" in captured["media"]
