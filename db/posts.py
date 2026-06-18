from datetime import datetime, timezone

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from db.schema import posted, sync_state


def is_posted(
    engine: Engine,
    source_network: str,
    source_post_id: str,
    destination_network: str,
) -> bool:
    stmt = (
        select(posted.c.id)
        .where(posted.c.source_network == source_network)
        .where(posted.c.source_post_id == source_post_id)
        .where(posted.c.destination_network == destination_network)
    )
    with engine.connect() as conn:
        return conn.execute(stmt).first() is not None


def mark_posted(
    engine: Engine,
    source_network: str,
    source_post_id: str,
    destination_network: str,
    destination_post_id: str,
    posted_at: datetime | None = None,
) -> bool:
    """Record a successful publish. Returns False if already recorded."""
    if is_posted(engine, source_network, source_post_id, destination_network):
        return False
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(posted).values(
                    source_network=source_network,
                    source_post_id=source_post_id,
                    destination_network=destination_network,
                    destination_post_id=destination_post_id,
                    posted_at=posted_at or datetime.now(timezone.utc),
                )
            )
        return True
    except IntegrityError:
        return False


def get_last_synced_at(
    engine: Engine, source_network: str, destination_network: str
) -> datetime | None:
    stmt = (
        select(sync_state.c.last_synced_at)
        .where(sync_state.c.source_network == source_network)
        .where(sync_state.c.destination_network == destination_network)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return row[0] if row else None


def set_last_synced_at(
    engine: Engine,
    source_network: str,
    destination_network: str,
    synced_at: datetime,
) -> None:
    existing = get_last_synced_at(engine, source_network, destination_network)
    with engine.begin() as conn:
        if existing is None:
            conn.execute(
                insert(sync_state).values(
                    source_network=source_network,
                    destination_network=destination_network,
                    last_synced_at=synced_at,
                )
            )
        else:
            conn.execute(
                sync_state.update()
                .where(sync_state.c.source_network == source_network)
                .where(sync_state.c.destination_network == destination_network)
                .values(last_synced_at=synced_at)
            )
