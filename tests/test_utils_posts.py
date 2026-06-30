from datetime import datetime, timezone

from utils.posts import sort_chronologically


def test_sort_chronologically_empty():
    assert sort_chronologically([]) == []


def test_sort_chronologically_orders_by_time(post_factory):
    older = post_factory("1", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    newer = post_factory("2", created_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert [p.id for p in sort_chronologically([newer, older])] == ["1", "2"]


def test_sort_chronologically_tiebreaks_numeric_ids(post_factory, utc_now):
    a = post_factory("10", created_at=utc_now)
    b = post_factory("2", created_at=utc_now)
    assert [p.id for p in sort_chronologically([a, b])] == ["2", "10"]


def test_sort_chronologically_tiebreaks_lexicographic_ids(post_factory, utc_now):
    a = post_factory("story_b", created_at=utc_now)
    b = post_factory("story_a", created_at=utc_now)
    assert [p.id for p in sort_chronologically([a, b])] == ["story_a", "story_b"]
