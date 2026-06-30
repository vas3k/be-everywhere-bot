from datetime import datetime, timezone

from datetime import datetime, timezone

from apis.twitter import (
    _best_video_url,
    _extract_media,
    _should_fetch_next_page,
    _skip_reason,
    _strip_trailing_links,
    _tweet_to_post,
    _twitter_media_type,
)
from apis.types import MediaItem, Post


def test_should_fetch_next_page_requires_full_page():
    since = datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc)
    tweets = [
        {"created_at": "2026-06-30T11:00:00Z"},
        {"created_at": "2026-06-30T10:30:00Z"},
    ]
    assert _should_fetch_next_page(tweets, since, page_size=10) is False


def test_should_fetch_next_page_when_all_newer_than_since():
    since = datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc)
    tweets = [{"created_at": "2026-06-30T11:00:00Z"} for _ in range(10)]
    assert _should_fetch_next_page(tweets, since, page_size=10) is True


def test_should_fetch_next_page_stops_when_any_tweet_is_old():
    since = datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc)
    tweets = [{"created_at": "2026-06-30T11:00:00Z"} for _ in range(9)]
    tweets.append({"created_at": "2026-06-30T09:00:00Z"})
    assert _should_fetch_next_page(tweets, since, page_size=10) is False


def test_best_video_url_picks_highest_bitrate():
    media = {
        "variants": [
            {"url": "https://video.test/low.mp4", "content_type": "video/mp4", "bit_rate": 100},
            {"url": "https://video.test/high.mp4", "content_type": "video/mp4", "bit_rate": 900},
        ]
    }
    assert _best_video_url(media) == "https://video.test/high.mp4"


def test_extract_media_photo_and_video():
    tweet = {
        "id": "1",
        "attachments": {"media_keys": ["photo1", "vid1"]},
    }
    includes = {
        "media": [
            {"media_key": "photo1", "type": "photo", "url": "https://pbs.twimg.com/a.jpg"},
            {
                "media_key": "vid1",
                "type": "video",
                "variants": [
                    {"url": "https://video.twimg.com/a.mp4", "content_type": "video/mp4", "bit_rate": 1}
                ],
            },
        ]
    }
    items = _extract_media(tweet, includes)
    assert len(items) == 2
    assert items[0].media_type == "photo"
    assert items[1].media_type == "video"


def test_strip_trailing_links_removes_tco_when_media():
    text = "Nice shot https://t.co/abc123"
    assert _strip_trailing_links(text, has_media=True) == "Nice shot"


def test_strip_trailing_links_removes_status_url():
    text = "Hello https://x.com/user/status/1"
    assert _strip_trailing_links(text, has_media=False) == "Hello"


def test_skip_reason_retweet():
    assert _skip_reason({"referenced_tweets": [{"type": "retweeted"}]}) == "retweet"


def test_skip_reason_quote_and_at_reply():
    assert _skip_reason({"referenced_tweets": [{"type": "quoted"}]}) == "quote"
    assert _skip_reason({"text": "@someone hi"}) == "at_reply"


def test_tweet_to_post_thread_fields():
    tweet = {
        "id": "100",
        "text": "reply body",
        "created_at": "2026-06-30T10:00:00Z",
        "conversation_id": "50",
        "referenced_tweets": [{"type": "replied_to", "id": "99"}],
        "in_reply_to_user_id": "42",
    }
    post = _tweet_to_post(tweet, {}, "42")
    assert post.id == "100"
    assert post.in_reply_to_id == "99"
    assert post.conversation_id == "50"
    assert post.is_thread_root is False



def test_twitter_media_type_mapping():
    assert _twitter_media_type(MediaItem(url="x", media_type="photo")) == (
        "image/jpeg",
        "tweet_image",
    )
    assert _twitter_media_type(MediaItem(url="x", media_type="animated_gif")) == (
        "video/mp4",
        "tweet_gif",
    )