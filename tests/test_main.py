from datetime import datetime, timezone

from config import WATCH_CRON
from main import parse_since, seconds_until_next_cron_run


def test_parse_since_date():
    dt = parse_since("2026-06-15")
    assert dt == datetime(2026, 6, 15, tzinfo=timezone.utc)


def test_parse_since_datetime():
    dt = parse_since("2026-06-15T10:30:00")
    assert dt.hour == 10
    assert dt.minute == 30


def test_seconds_until_next_cron_run():
    now = datetime(2026, 6, 30, 9, 15, tzinfo=timezone.utc)
    delay, next_run = seconds_until_next_cron_run(WATCH_CRON, now=now)
    assert next_run.hour == 9
    assert next_run.minute == 30
    assert delay == 15 * 60

