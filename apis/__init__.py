"""Shared function signatures every social network API module implements.

Each module in apis/ exposes:
  - authenticate(engine, label) -> Account
  - fetch_posts(engine, account_id, since, include_replies, max_pages) -> list[Post]
  - download_media(media, engine, account_id) -> bytes
  - publish_outbound(engine, account_id, outbound, media_bytes, *, reply_to) -> PublishResult

Source-only networks raise NotImplementedError from publish_outbound.
Reply-to-other-people filtering runs in sync/engine after fetch, not in APIs.
Shared helpers live in utils/.
"""

from datetime import datetime

from sqlalchemy.engine import Engine

from apis.types import MediaItem, Post, PublishResult

__all__ = [
    "Post",
    "MediaItem",
    "PublishResult",
    "Engine",
    "datetime",
]
