from datetime import datetime, timedelta, timezone

from apis.instagram import (
    STORY_ID_PREFIX,
    _assign_story_groups,
    _extract_media,
    _feed_item_to_post,
    build_story_outbounds,
    is_story_batch,
)
from apis.types import MediaItem
from config import TELEGRAM_LIMITS, NetworkLimits


def test_extract_media_image():
    items = _extract_media(
        {"media_type": "IMAGE", "media_url": "https://cdn.example/1.jpg"}
    )
    assert len(items) == 1
    assert items[0].media_type == "photo"


def test_extract_media_carousel():
    items = _extract_media(
        {
            "media_type": "CAROUSEL_ALBUM",
            "children": {
                "data": [
                    {"media_type": "IMAGE", "media_url": "https://cdn.example/1.jpg"},
                    {"media_type": "VIDEO", "media_url": "https://cdn.example/2.mp4"},
                ]
            },
        }
    )
    assert len(items) == 2
    assert items[0].media_type == "photo"
    assert items[1].media_type == "video"


def test_feed_item_to_post_with_caption():
    post = _feed_item_to_post(
        {
            "id": "123",
            "caption": "Hello Instagram",
            "timestamp": "2026-06-30T10:00:00+0000",
            "media_type": "IMAGE",
            "media_url": "https://cdn.example/1.jpg",
        },
        "user-1",
    )
    assert post is not None
    assert post.text == "Hello Instagram"
    assert post.id == "post_123"
    assert len(post.media) == 1


def test_assign_story_groups_clusters_within_window(post_factory, utc_now):
    stories = [
        post_factory(
            f"{STORY_ID_PREFIX}{i}",
            created_at=utc_now + timedelta(minutes=i * 10),
            media=[MediaItem(url=f"https://x/{i}", media_type="photo")],
        )
        for i in range(3)
    ]
    grouped = _assign_story_groups(stories, window_minutes=60)
    assert len({s.conversation_id for s in grouped}) == 1


def test_assign_story_groups_splits_on_large_gap(post_factory, utc_now):
    stories = [
        post_factory(
            f"{STORY_ID_PREFIX}1",
            created_at=utc_now,
            media=[MediaItem(url="https://x/1", media_type="photo")],
        ),
        post_factory(
            f"{STORY_ID_PREFIX}2",
            created_at=utc_now + timedelta(minutes=90),
            media=[MediaItem(url="https://x/2", media_type="photo")],
        ),
    ]
    grouped = _assign_story_groups(stories, window_minutes=60)
    assert len({s.conversation_id for s in grouped}) == 2


def test_is_story_batch(post_factory):
    stories = [
        post_factory(f"{STORY_ID_PREFIX}1"),
        post_factory(f"{STORY_ID_PREFIX}2"),
    ]
    mixed = [post_factory("post_1"), post_factory(f"{STORY_ID_PREFIX}2")]
    assert is_story_batch(stories) is True
    assert is_story_batch(mixed) is False
    assert is_story_batch([]) is False


def test_build_story_outbounds_merges_for_multi_media_dest(post_factory, utc_now):
    batch = [
        post_factory(
            f"{STORY_ID_PREFIX}{i}",
            created_at=utc_now + timedelta(minutes=i),
            media=[MediaItem(url=f"https://x/{i}", media_type="photo")],
        )
        for i in range(3)
    ]
    out = build_story_outbounds(batch, TELEGRAM_LIMITS)
    assert len(out) == 1
    assert len(out[0].media) == 3
    assert len(out[0].source_post_ids) == 3


def test_build_story_outbounds_splits_when_dest_allows_one_media(post_factory, utc_now):
    batch = [
        post_factory(
            f"{STORY_ID_PREFIX}{i}",
            created_at=utc_now + timedelta(minutes=i),
            media=[MediaItem(url=f"https://x/{i}", media_type="photo")],
        )
        for i in range(2)
    ]
    single = NetworkLimits(max_text=500, max_caption=500, max_media_group=1)
    out = build_story_outbounds(batch, single)
    assert len(out) == 2
    assert all(len(o.media) == 1 for o in out)
