from datetime import datetime, timedelta, timezone

import pytest

from apis.types import MediaItem, OutboundPost
from config import NETWORK_LIMITS, NETWORK_MASTODON, NETWORK_TELEGRAM, TELEGRAM_LIMITS
from sync.thread_processor import (
    build_outbound_posts,
    collect_ready_batch,
    get_network_limits,
    is_old_enough,
    split_text,
)


def test_split_text_empty():
    assert split_text("", 280) == []


def test_split_text_short_enough():
    assert split_text("hello", 280) == ["hello"]


def test_split_text_invalid_max_len():
    with pytest.raises(ValueError):
        split_text("hello", 0)


def test_split_text_prefers_paragraph_break():
    text = "First paragraph.\n\nSecond paragraph is longer."
    chunks = split_text(text, 20)
    assert chunks[0] == "First paragraph."
    assert "Second" in chunks[1]


def test_split_text_prefers_sentence_break():
    text = "One sentence here. Another sentence follows."
    chunks = split_text(text, 22)
    assert chunks[0].endswith(".")
    assert chunks[1].startswith("Another")


def test_split_text_splits_long_word_at_limit():
    word = "x" * 30
    assert split_text(word, 10) == [word[:10], word[10:20], word[20:]]


def test_build_outbound_posts_text_only(post_factory):
    post = post_factory("1", text="Short post")
    out = build_outbound_posts([post], TELEGRAM_LIMITS)
    assert len(out) == 1
    assert out[0].text == "Short post"
    assert out[0].source_post_ids == ["1"]


def test_build_outbound_posts_splits_long_text(post_factory):
    text = "word " * 200
    post = post_factory("1", text=text.strip())
    out = build_outbound_posts([post], NETWORK_LIMITS[NETWORK_MASTODON])
    assert len(out) > 1
    assert all(len(chunk.text) <= 500 for chunk in out)
    assert all(chunk.source_post_ids == ["1"] for chunk in out)


def test_build_outbound_posts_chunks_media_with_caption(post_factory, photo):
    media = [photo] * 5
    post = post_factory("1", text="Caption", media=media)
    out = build_outbound_posts([post], TELEGRAM_LIMITS)
    assert len(out) == 2
    assert out[0].text == "Caption"
    assert len(out[0].media) == 4
    assert len(out[1].media) == 1
    assert out[1].text == ""


def test_build_outbound_posts_spills_long_caption_to_followups(post_factory, photo):
    caption = "a" * 2000
    post = post_factory("1", text=caption, media=[photo])
    out = build_outbound_posts([post], TELEGRAM_LIMITS)
    assert len(out[0].media) == 1
    assert len(out[0].text) <= TELEGRAM_LIMITS.max_caption
    assert len(out) > 1
    assert any(chunk.text for chunk in out[1:])


def test_build_outbound_posts_multiple_source_posts(post_factory, utc_now):
    first = post_factory("1", text="one", created_at=utc_now)
    second = post_factory(
        "2",
        text="two",
        created_at=utc_now + timedelta(minutes=1),
        conversation_id="thread",
    )
    out = build_outbound_posts([second, first], TELEGRAM_LIMITS)
    assert [o.text for o in out] == ["one", "two"]


def test_build_outbound_posts_empty_input():
    assert build_outbound_posts([]) == []


def test_get_network_limits_known_network():
    assert get_network_limits(NETWORK_TELEGRAM).max_media_group == 4


def test_get_network_limits_unknown_network():
    with pytest.raises(ValueError, match="No limits configured"):
        get_network_limits("unknown")


def test_is_old_enough(post_factory):
    now = datetime.now(timezone.utc)
    old = post_factory("1", created_at=now - timedelta(minutes=60))
    young = post_factory("2", created_at=now - timedelta(minutes=5))
    assert is_old_enough(old, 30) is True
    assert is_old_enough(young, 30) is False


def test_collect_ready_batch_skips_synced(post_factory):
    now = datetime.now(timezone.utc)
    posts = [
        post_factory("1", created_at=now - timedelta(hours=1)),
        post_factory("2", created_at=now - timedelta(minutes=50)),
    ]
    batch = collect_ready_batch(
        posts,
        is_synced=lambda pid: pid == "1",
        enforce_min_age=True,
        min_age_minutes=30,
    )
    assert [p.id for p in batch] == ["2"]


def test_collect_ready_batch_stops_at_young_post(post_factory):
    now = datetime.now(timezone.utc)
    posts = [
        post_factory("1", created_at=now - timedelta(hours=1)),
        post_factory("2", created_at=now - timedelta(minutes=5)),
    ]
    batch = collect_ready_batch(
        posts,
        is_synced=lambda _pid: False,
        enforce_min_age=True,
        min_age_minutes=30,
    )
    assert [p.id for p in batch] == ["1"]


def test_collect_ready_batch_collects_older_posts_before_young_gate(post_factory):
    now = datetime.now(timezone.utc)
    posts = [
        post_factory("1", created_at=now - timedelta(hours=2)),
        post_factory("2", created_at=now - timedelta(hours=1)),
        post_factory("3", created_at=now - timedelta(minutes=5)),
    ]
    batch = collect_ready_batch(
        posts,
        is_synced=lambda _pid: False,
        enforce_min_age=True,
        min_age_minutes=30,
    )
    assert [p.id for p in batch] == ["1", "2"]


def test_collect_ready_batch_ignores_min_age_when_disabled(post_factory):
    now = datetime.now(timezone.utc)
    young = post_factory("1", created_at=now - timedelta(minutes=5))
    batch = collect_ready_batch(
        [young],
        is_synced=lambda _pid: False,
        enforce_min_age=False,
        min_age_minutes=30,
    )
    assert [p.id for p in batch] == ["1"]
