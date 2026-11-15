from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from apis.threads import (
    _create_container,
    _extract_media,
    _item_to_post,
    _partition_public_media,
    _skip_reason,
    _strip_trailing_links,
    _threads_media_type,
    fetch_posts,
)
from apis.types import MediaItem
from db.accounts import create_account, get_all_credentials, set_credentials
from utils.meta_tokens import THREADS_TOKEN


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


def test_partition_public_media_splits_types():
    media = [
        MediaItem(url="https://cdn/a.jpg", media_type="photo"),
        MediaItem(url="https://cdn/b.mp4", media_type="video"),
    ]
    photos, videos = _partition_public_media(media)
    assert len(photos) == 1
    assert len(videos) == 1


@pytest.mark.asyncio
async def test_create_container_rejects_mixed_photo_video():
    media = [
        MediaItem(url="https://cdn/a.jpg", media_type="photo"),
        MediaItem(url="https://cdn/b.mp4", media_type="video"),
    ]
    with pytest.raises(RuntimeError, match="mixed photos and videos"):
        await _create_container("token", "123", text="", media=media)


@pytest.mark.asyncio
@respx.mock
async def test_create_container_carousel_waits_for_children():
    user_id = "123"
    post_n = {"n": 0}

    def on_post(request: httpx.Request) -> httpx.Response:
        post_n["n"] += 1
        body = request.content.decode()
        if "CAROUSEL" in body:
            return httpx.Response(200, json={"id": "carousel-parent"})
        return httpx.Response(200, json={"id": f"child-{post_n['n']}"})

    respx.post(f"https://graph.threads.net/v1.0/{user_id}/threads").mock(
        side_effect=on_post
    )
    respx.get(url__regex=r"https://graph\.threads\.net/v1\.0/.*").mock(
        return_value=httpx.Response(200, json={"status": "FINISHED"})
    )

    media = [
        MediaItem(url="https://cdn/a.jpg", media_type="photo"),
        MediaItem(url="https://cdn/b.jpg", media_type="photo"),
    ]
    container_id = await _create_container("token", user_id, text="hi", media=media)
    assert container_id == "carousel-parent"
    assert post_n["n"] == 3


@pytest.mark.asyncio
@respx.mock
async def test_fetch_posts_renews_expiring_token_before_calling_api(engine):
    account = create_account(engine, "threads", "default", "123")
    near_expiry = datetime.now(timezone.utc) + timedelta(days=2)
    set_credentials(
        engine,
        account.id,
        {
            "access_token": "stale-token",
            "user_id": "123",
            "username": "vas3k",
            "token_expires_at": near_expiry.isoformat(),
        },
    )

    respx.get(THREADS_TOKEN.refresh.url).mock(
        return_value=httpx.Response(
            200, json={"access_token": "fresh-token", "expires_in": 5183944}
        )
    )
    fetch = respx.get(url__regex=r"https://graph\.threads\.net/v1\.0/123/.*").mock(
        return_value=httpx.Response(200, json={"data": [], "paging": {}})
    )

    posts = await fetch_posts(engine, account.id)

    assert posts == []
    assert all(
        call.request.url.params["access_token"] == "fresh-token"
        for call in fetch.calls
    )
    assert get_all_credentials(engine, account.id)["access_token"] == "fresh-token"


@pytest.mark.parametrize("metadata", [
    {"is_reply": True},
    {"is_reply": True, "root_post": {"id": "100"}},
    {"is_reply": True, "replied_to": {"id": "100"}},
    {"replied_to": "100"},
    {"root_post": "100"},
    {"is_reply": False, "replied_to": {"id": "100"}},
])
def test_incomplete_or_conflicting_reply_metadata_never_becomes_an_original(metadata):
    from utils.filters import filter_own_threads

    post = _item_to_post({
        "id": "200", "text": "Reply without an @mention",
        "timestamp": "2026-06-30T12:00:00+0000", **metadata,
    }, "user-1")

    assert post.is_reply is True
    assert post.is_thread_root is False
    assert post.in_reply_to_user_id is None
    assert filter_own_threads([post]) == []


def test_absent_reply_metadata_remains_unknown_instead_of_becoming_an_original():
    post = _item_to_post({
        "id": "200", "text": "Could be a reply",
        "timestamp": "2026-06-30T12:00:00+0000",
    }, "user-1")

    assert post.is_reply is None
    assert post.is_thread_root is False
