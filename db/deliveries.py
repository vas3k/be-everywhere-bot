from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Engine

from apis.types import MediaItem, OutboundPost, PublishResult
from db.schema import mirrored_posts, sync_batches, sync_mappings


@dataclass
class Delivery:
    source_account_id: int
    dest_account_id: int
    conversation_id: str
    outbounds: list[OutboundPost]
    dest_ids: list[str]
    reply_ref: str | None


def load_deliveries(engine: Engine, source_account_id: int) -> list[Delivery]:
    with engine.connect() as connection:
        rows = connection.execute(select(sync_batches).where(
            sync_batches.c.source_account_id == source_account_id,
        )).mappings().all()

    deliveries = []
    for row in rows:
        outbounds = [OutboundPost(
            text=item["text"], source_post_ids=item["source_post_ids"],
            media=[MediaItem(**media) for media in item["media"]],
        ) for item in row["outbounds"]]
        deliveries.append(Delivery(**{**row, "outbounds": outbounds}))
    return deliveries


def create_delivery(engine: Engine, delivery: Delivery) -> None:
    with engine.begin() as connection:
        connection.execute(insert(sync_batches).values(**asdict(delivery)))


def record_delivery_part(engine: Engine, delivery: Delivery, result: PublishResult) -> None:
    dest_ids = [*delivery.dest_ids, result.post_id]
    with engine.begin() as connection:
        connection.execute(update(sync_batches).where(
            sync_batches.c.source_account_id == delivery.source_account_id,
            sync_batches.c.dest_account_id == delivery.dest_account_id,
            sync_batches.c.conversation_id == delivery.conversation_id,
        ).values(dest_ids=dest_ids, reply_ref=result.reply_ref))
        for post_id in result.mirrored_ids or (result.post_id,):
            connection.execute(insert(mirrored_posts).values(
                account_id=delivery.dest_account_id, post_id=post_id,
                created_at=datetime.now(timezone.utc),
            ).on_conflict_do_nothing(index_elements=["account_id", "post_id"]))
    delivery.dest_ids = dest_ids
    delivery.reply_ref = result.reply_ref


def complete_delivery(engine: Engine, delivery: Delivery) -> int:
    if len(delivery.dest_ids) != len(delivery.outbounds):
        raise ValueError("Cannot complete an unfinished delivery")

    destinations = {}
    for outbound, dest_id in zip(delivery.outbounds, delivery.dest_ids):
        for source_id in outbound.source_post_ids:
            destinations[source_id] = dest_id

    count = 0
    with engine.begin() as connection:
        for source_id, dest_id in destinations.items():
            result = connection.execute(insert(sync_mappings).values(
                source_account_id=delivery.source_account_id,
                source_post_id=source_id, dest_account_id=delivery.dest_account_id,
                dest_post_id=dest_id, synced_at=datetime.now(timezone.utc),
            ).on_conflict_do_nothing(index_elements=[
                "source_account_id", "source_post_id", "dest_account_id",
            ]))
            count += result.rowcount
        connection.execute(delete(sync_batches).where(
            sync_batches.c.source_account_id == delivery.source_account_id,
            sync_batches.c.dest_account_id == delivery.dest_account_id,
            sync_batches.c.conversation_id == delivery.conversation_id,
        ))
    return count
