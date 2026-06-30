from datetime import datetime, timezone

from apis.bluesky import (
    _did_from_uri,
    _extract_media,
    _feed_item_to_post,
    _is_token_expired,
    _rkey_from_uri,
    _skip_reason,
    _xrpc_url,
    filter_originals_and_threads,
)
from apis.types import Post


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


def test_skip_reason_repost_quote_foreign_reply():
    own_did = "did:plc:me"
    assert _skip_reason({"reason": {"$type": "app.bsky.feed.defs#reasonRepost"}}, own_did) == "repost"
    item = {
        "post": {
            "record": {
                "text": "quoted",
                "embed": {"$type": "app.bsky.embed.record"},
            }
        }
    }
    assert _skip_reason(item, own_did) == "quote"
    foreign = {
        "post": {
            "record": {
                "reply": {"root": {"uri": "at://did:plc:other/app.bsky.feed.post/x"}},
            }
        }
    }
    assert _skip_reason(foreign, own_did) == "foreign_reply"


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


def test_filter_originals_keeps_own_thread():
    did = "did:plc:me"
    root = Post(
        id="root1",
        text="root",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="root1",
        author_id=did,
        is_thread_root=True,
    )
    own_reply = Post(
        id="reply1",
        text="mine",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="root1",
        author_id=did,
        in_reply_to_id="root1",
    )
    foreign = Post(
        id="reply2",
        text="other",
        created_at=datetime(2026, 6, 30, tzinfo=timezone.utc),
        conversation_id="other",
        author_id=did,
        in_reply_to_id="someone",
    )
    kept = filter_originals_and_threads([root, own_reply, foreign])
    assert [p.id for p in kept] == ["root1", "reply1"]
