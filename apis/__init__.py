"""Shared function signatures every social network API module implements.

Each module in apis/ exposes the same async functions:
  - authenticate(engine) -> None
  - fetch_posts(engine, since, include_replies) -> list[Post]
  - publish_post(engine, post, media_bytes) -> str
  - download_media(media, access_token) -> bytes
"""

from datetime import datetime

from sqlalchemy.engine import Engine

from apis.types import MediaItem, Post

__all__ = [
    "Post",
    "MediaItem",
    "Engine",
    "datetime",
]
