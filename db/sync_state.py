from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from db.schema import account_sync_state, mirrored_posts, sync_mappings


def is_synced(
    engine: Engine,
    source_account_id: int,
    source_post_id: str,
    dest_account_id: int,
) -> bool:
    stmt = (
        select(sync_mappings.c.id)
        .where(sync_mappings.c.source_account_id == source_account_id)
        .where(sync_mappings.c.source_post_id == source_post_id)
        .where(sync_mappings.c.dest_account_id == dest_account_id)
    )
    with engine.connect() as conn:
        return conn.execute(stmt).first() is not None


def get_dest_post_id(
    engine: Engine,
    source_account_id: int,
    source_post_id: str,
    dest_account_id: int,
) -> str | None:
    stmt = (
        select(sync_mappings.c.dest_post_id)
        .where(sync_mappings.c.source_account_id == source_account_id)
        .where(sync_mappings.c.source_post_id == source_post_id)
        .where(sync_mappings.c.dest_account_id == dest_account_id)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return row[0] if row else None


def mark_synced(
    engine: Engine,
    source_account_id: int,
    source_post_id: str,
    dest_account_id: int,
    dest_post_id: str,
    synced_at: datetime | None = None,
) -> bool:
    """Record a successful sync. Returns False if already recorded."""
    if is_synced(engine, source_account_id, source_post_id, dest_account_id):
        return False
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(sync_mappings).values(
                    source_account_id=source_account_id,
                    source_post_id=source_post_id,
                    dest_account_id=dest_account_id,
                    dest_post_id=dest_post_id,
                    synced_at=synced_at or datetime.now(timezone.utc),
                )
            )
        return True
    except IntegrityError:
        return False


def record_mirrored_post(
    engine: Engine,
    account_id: int,
    post_id: str,
    created_at: datetime | None = None,
) -> None:
    """Mark a post on an account as created by sync (skip on future fetches)."""
    stmt = (
        select(mirrored_posts.c.id)
        .where(mirrored_posts.c.account_id == account_id)
        .where(mirrored_posts.c.post_id == post_id)
    )
    with engine.connect() as conn:
        if conn.execute(stmt).first() is not None:
            return
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(mirrored_posts).values(
                    account_id=account_id,
                    post_id=post_id,
                    created_at=created_at or datetime.now(timezone.utc),
                )
            )
    except IntegrityError:
        pass


def get_mirrored_post_ids(engine: Engine, account_id: int) -> set[str]:
    stmt = select(mirrored_posts.c.post_id).where(
        mirrored_posts.c.account_id == account_id
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return {row[0] for row in rows}


def get_last_synced_at(engine: Engine, account_id: int) -> datetime | None:
    stmt = select(account_sync_state.c.last_synced_at).where(
        account_sync_state.c.account_id == account_id
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return row[0] if row else None


def set_last_synced_at(
    engine: Engine, account_id: int, synced_at: datetime
) -> None:
    existing = get_last_synced_at(engine, account_id)
    with engine.begin() as conn:
        if existing is None:
            conn.execute(
                insert(account_sync_state).values(
                    account_id=account_id,
                    last_synced_at=synced_at,
                )
            )
        else:
            conn.execute(
                update(account_sync_state)
                .where(account_sync_state.c.account_id == account_id)
                .values(last_synced_at=synced_at)
            )
