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
    _message_to_post,
    _retry_after_seconds,
    _send_method,
    publish_outbound,
)
from apis.types import MediaItem, OutboundPost
from config import NETWORK_TELEGRAM, TELEGRAM_LIMITS
from utils.thread_processor import build_outbound_posts
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
    merged = build_outbound_posts([slide2, slide1], TELEGRAM_LIMITS)
    assert len(merged) == 1
    assert merged[0].source_post_ids == ["10", "11"]
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


@respx.mock
async def test_shared_bot_saves_every_channel_before_acknowledging_updates(engine):
    from apis.telegram import fetch_posts
    from db.accounts import get_all_credentials
    from db.posts import load_posts

    first = create_account(engine, "telegram", "first", "@first")
    second = create_account(engine, "telegram", "second", "@second")
    for account, channel in ((first, "@first"), (second, "@second")):
        set_credentials(engine, account.id, {"bot_token": "shared", "channel_id": channel, "update_offset": "0"})
    updates = [
        {"update_id": 1, "channel_post": {"message_id": 10, "date": 1782813600, "text": "First channel", "chat": {"id": 1, "username": "first"}}},
        {"update_id": 2, "channel_post": {"message_id": 20, "date": 1782813600, "text": "Second channel", "chat": {"id": 2, "username": "second"}}},
    ]

    def telegram_response(request):
        if request.url.params["offset"] == "0":
            return httpx.Response(200, json={"ok": True, "result": updates})
        assert [post.text for post in load_posts(engine, first.id)] == ["First channel"]
        assert [post.text for post in load_posts(engine, second.id)] == ["Second channel"]
        assert get_all_credentials(engine, second.id)["update_offset"] == "3"
        return httpx.Response(200, json={"ok": True, "result": []})

    route = respx.get("https://api.telegram.org/botshared/getUpdates").mock(side_effect=telegram_response)
    posts = await fetch_posts(engine, first.id)
    assert [post.text for post in posts] == ["First channel"]
    assert route.call_count == 2
    assert await fetch_posts(engine, second.id) == []
    assert [post.text for post in load_posts(engine, second.id)] == ["Second channel"]


@respx.mock
async def test_telegram_retains_downloaded_page_if_next_page_fails(engine):
    from apis.telegram import fetch_posts
    from db.posts import load_posts

    account = create_account(engine, "telegram", "main", "@channel")
    set_credentials(engine, account.id, {"bot_token": "token", "channel_id": "@channel"})
    respx.get("https://api.telegram.org/bottoken/getUpdates").mock(side_effect=[
        httpx.Response(200, json={"result": [{"update_id": 1, "channel_post": {
            "message_id": 10, "date": 1782813600, "text": "Saved first", "chat": {"username": "channel"},
        }}]}),
        httpx.Response(503),
    ])

    with pytest.raises(httpx.HTTPStatusError):
        await fetch_posts(engine, account.id)
    assert [post.text for post in load_posts(engine, account.id)] == ["Saved first"]


@respx.mock
async def test_shared_destination_updates_are_acknowledged_without_becoming_source_posts(engine):
    from apis.telegram import fetch_posts
    from db.accounts import get_account
    from db.posts import load_posts
    from db.projects import create_project
    from db.sync_state import get_mirrored_post_ids, record_mirrored_post

    project = create_project(engine, "Work")
    first = create_account(engine, "telegram", "main", "@Channel", sync_role="destination_only")
    second = create_account(engine, "telegram", "main", "-100123", project_id=project.id, sync_role="destination_only")
    reader = create_account(engine, "telegram", "reader", "@reader", sync_role="source_only")
    for account, channel in ((first, "@channel"), (second, "-100123"), (reader, "@reader")):
        set_credentials(engine, account.id, {"bot_token": "shared", "channel_id": channel})
    record_mirrored_post(engine, first.id, "10")
    respx.get("https://api.telegram.org/botshared/getUpdates").mock(side_effect=[
        httpx.Response(200, json={"result": [{"update_id": 1, "channel_post": {
            "message_id": 10, "date": 1782813600, "text": "Mirrored", "chat": {"id": -100123, "username": "channel"},
        }}]}),
        httpx.Response(200, json={"result": []}),
    ])

    await fetch_posts(engine, reader.id)

    assert load_posts(engine, first.id) == load_posts(engine, second.id) == []
    assert get_account(engine, first.id).remote_id == get_account(engine, second.id).remote_id == "-100123"
    assert get_mirrored_post_ids(engine, second.id) == {"10"}
