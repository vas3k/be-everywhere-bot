from dataclasses import asdict
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, Engine

from apis.types import MediaItem, Post
from db.schema import source_posts


def save_posts(connection: Connection, account_id: int, posts: list[Post]) -> None:
    for post in posts:
        payload = asdict(post)
        payload["created_at"] = post.created_at.isoformat()
        statement = insert(source_posts).values(
            account_id=account_id, post_id=post.id, payload=payload,
        )
        connection.execute(statement.on_conflict_do_update(
            index_elements=["account_id", "post_id"],
            set_={"payload": statement.excluded.payload},
        ))


def load_posts(engine: Engine, account_id: int) -> list[Post]:
    with engine.connect() as connection:
        payloads = connection.execute(
            select(source_posts.c.payload).where(source_posts.c.account_id == account_id)
        ).scalars().all()

    posts = []
    for payload in payloads:
        posts.append(Post(
            **{key: value for key, value in payload.items() if key not in ("created_at", "media")},
            created_at=datetime.fromisoformat(payload["created_at"]),
            media=[MediaItem(**item) for item in payload["media"]],
        ))
    return posts


def discard_posts(engine: Engine, account_id: int, post_ids: list[str]) -> None:
    if not post_ids:
        return
    with engine.begin() as connection:
        connection.execute(delete(source_posts).where(
            source_posts.c.account_id == account_id,
            source_posts.c.post_id.in_(post_ids),
        ))
