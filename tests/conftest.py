from datetime import datetime, timezone

import pytest

from apis.types import MediaItem, Post
from db.connection import get_engine


@pytest.fixture
def engine(tmp_path):
    return get_engine(tmp_path / "test.db")


@pytest.fixture
def utc_now() -> datetime:
    return datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def post_factory(utc_now):
    def _make_post(
        post_id: str = "1",
        *,
        text: str = "hello",
        created_at: datetime | None = None,
        conversation_id: str | None = None,
        media: list[MediaItem] | None = None,
        in_reply_to_id: str | None = None,
    ) -> Post:
        return Post(
            id=post_id,
            text=text,
            created_at=created_at or utc_now,
            conversation_id=conversation_id or post_id,
            author_id="author",
            media=media or [],
            in_reply_to_id=in_reply_to_id,
        )

    return _make_post


@pytest.fixture
def photo(url: str = "https://example.com/a.jpg") -> MediaItem:
    return MediaItem(url=url, media_type="photo")


@pytest.fixture
def video(url: str = "https://example.com/a.mp4") -> MediaItem:
    return MediaItem(url=url, media_type="video")
