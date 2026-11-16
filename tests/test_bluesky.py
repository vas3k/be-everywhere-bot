from datetime import datetime, timezone

import pytest

from apis.bluesky import (
    _build_embed,
    _did_from_uri,
    _extract_media,
    _feed_item_to_post,
    _is_token_expired,
    _rkey_from_uri,
    _skip_reason,
    _xrpc_url,
)
from apis.types import MediaItem, Post
from config import (
    NETWORK_BLUESKY,
    NETWORK_LIMITS,
    NETWORK_LINKEDIN,
    NETWORK_MASTODON,
    NETWORK_THREADS,
)


def test_rkey_and_did_from_uri():
    uri = "at://did:plc:abc/app.bsky.feed.post/3jx7ytmdwej2k"
    assert _rkey_from_uri(uri) == "3jx7ytmdwej2k"
    assert _did_from_uri(uri) == "did:plc:abc"


def test_xrpc_url():
    assert _xrpc_url("https://bsky.social", "app.bsky.feed.getAuthorFeed").endswith(
        "/xrpc/app.bsky.feed.getAuthorFeed"
    )


def test_is_token_expired():
    assert _is_token_expired(400, {"message": "Token has expired"}) is True
    assert _is_token_expired(401, {"error": "Invalid token"}) is True
    assert _is_token_expired(404, {"message": "Not found"}) is False


def test_extract_media_images_and_video():
    images_embed = {
        "$type": "app.bsky.embed.images#view",
        "images": [{"fullsize": "https://cdn/img.jpg", "alt": "alt"}],
    }
    imgs = _extract_media(images_embed)
    assert len(imgs) == 1
    assert imgs[0].media_type == "photo"
    assert imgs[0].alt_text == "alt"

    video_embed = {
        "$type": "app.bsky.embed.video#view",
        "playlist": "https://cdn/vid.m3u8",
    }
    vids = _extract_media(video_embed)
    assert vids[0].media_type == "video"


def test_skip_reason_repost_and_quote():
    assert _skip_reason({"reason": {"$type": "app.bsky.feed.defs#reasonRepost"}}) == "repost"
    item = {
        "post": {
            "record": {
                "text": "quoted",
                "embed": {"$type": "app.bsky.embed.record"},
            }
        }
    }
    assert _skip_reason(item) == "quote"


def test_feed_item_to_post_reply_chain():
    own_did = "did:plc:me"
    item = {
        "post": {
            "uri": "at://did:plc:me/app.bsky.feed.post/reply1",
            "record": {
                "text": "reply text",
                "createdAt": "2026-06-30T10:00:00.000Z",
                "reply": {
                    "root": {"uri": "at://did:plc:me/app.bsky.feed.post/root1"},
                    "parent": {"uri": "at://did:plc:me/app.bsky.feed.post/root1"},
                },
            },
        }
    }
    post = _feed_item_to_post(item, own_did)
    assert post.id == "reply1"
    assert post.conversation_id == "root1"
    assert post.in_reply_to_id == "root1"


@pytest.mark.asyncio
async def test_build_embed_rejects_mixed_photo_video(monkeypatch):
    async def fake_upload(_session, _raw, _item):
        return {"$type": "blob", "ref": {"$link": "abc"}}

    monkeypatch.setattr("apis.bluesky._upload_blob", fake_upload)

    media = [
        MediaItem(url="https://cdn/a.jpg", media_type="photo"),
        MediaItem(url="https://cdn/b.mp4", media_type="video"),
    ]
    with pytest.raises(RuntimeError, match="mixed photos and videos"):
        await _build_embed(None, media, [b"a", b"b"], log_id="1")


@pytest.mark.asyncio
async def test_build_embed_caps_images_at_four(monkeypatch):
    uploaded: list[str] = []

    async def fake_upload(_session, _raw, item):
        uploaded.append(item.media_type)
        return {"$type": "blob", "ref": {"$link": "abc"}}

    monkeypatch.setattr("apis.bluesky._upload_blob", fake_upload)

    media = [MediaItem(url=f"https://cdn/{i}.jpg", media_type="photo") for i in range(6)]
    embed = await _build_embed(None, media, [b"x"] * 6, log_id="1")

    assert embed["$type"] == "app.bsky.embed.images"
    assert len(embed["images"]) == 4
    assert len(uploaded) == 4


def test_networks_that_disallow_mixed_media():
    disallow = {
        NETWORK_BLUESKY,
        NETWORK_MASTODON,
        NETWORK_THREADS,
        NETWORK_LINKEDIN,
    }
    for network, limits in NETWORK_LIMITS.items():
        if network in disallow:
            assert limits.allows_mixed_media is False, network
        else:
            assert limits.allows_mixed_media is True, network


@pytest.mark.asyncio
@pytest.mark.parametrize("text,urls", [
    ("Привет 🌍 https://vas3k.club/post/123/", ["https://vas3k.club/post/123/"]),
    ("https://example.org/a, ещё (https://example.org/b).", ["https://example.org/a", "https://example.org/b"]),
    ("https://example.org/путь https://example.org/путь", ["https://example.org/путь", "https://example.org/путь"]),
    ("Текст без ссылки 🌍", []),
])
async def test_publish_marks_links_with_utf8_byte_ranges(monkeypatch, text, urls):
    from unittest.mock import AsyncMock
    from apis import bluesky
    from apis.types import OutboundPost

    session = AsyncMock()
    session.did = "did:plc:test"
    session.post_json.return_value = {"uri": "at://did:plc:test/app.bsky.feed.post/abc", "cid": "cid"}
    monkeypatch.setattr(bluesky.BlueskySession, "load", lambda *args: session)
    await bluesky.publish_outbound(None, 1, OutboundPost(text, [], ["source"]))
    record = session.post_json.call_args.args[1]["record"]
    facets = record.get("facets", [])
    assert len(facets) == len(urls)
    encoded = text.encode("utf-8")
    for facet, url in zip(facets, urls):
        bounds = facet["index"]
        assert encoded[bounds["byteStart"]:bounds["byteEnd"]].decode("utf-8") == url
        assert facet["features"] == [{"$type": "app.bsky.richtext.facet#link", "uri": url}]
    assert record["text"] == text
