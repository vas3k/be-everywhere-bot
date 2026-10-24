import pytest
from sqlalchemy import event

from apis.types import MediaItem, OutboundPost, PublishResult
from db.accounts import create_account
from db.deliveries import Delivery, complete_delivery, create_delivery, load_deliveries, record_delivery_part
from db.sync_state import get_dest_post_id, get_mirrored_post_ids


def test_delivery_round_trip_preserves_content_and_progress(engine):
    source = create_account(engine, "twitter", "main", "source")
    dest = create_account(engine, "telegram", "main", "dest")
    delivery = Delivery(source.id, dest.id, "thread", [
        OutboundPost("Caption", [MediaItem("tgfile:photo", "photo", "Alt text")], ["1", "2"]),
        OutboundPost("Tail", source_post_ids=["1", "2"]),
    ], [], None)
    create_delivery(engine, delivery)
    record_delivery_part(engine, delivery, PublishResult("first", "reply-first", ("first", "album-second")))

    restored, = load_deliveries(engine, source.id)
    assert restored == delivery
    assert get_mirrored_post_ids(engine, dest.id) == {"first", "album-second"}
    assert get_dest_post_id(engine, source.id, "1", dest.id) is None

    record_delivery_part(engine, restored, PublishResult("last", "reply-last"))
    assert complete_delivery(engine, restored) == 2
    assert get_dest_post_id(engine, source.id, "1", dest.id) == "last"
    assert get_dest_post_id(engine, source.id, "2", dest.id) == "last"
    assert load_deliveries(engine, source.id) == []


def test_unfinished_delivery_cannot_be_marked_complete(engine):
    source = create_account(engine, "twitter", "main", "source")
    dest = create_account(engine, "telegram", "main", "dest")
    delivery = Delivery(source.id, dest.id, "thread", [OutboundPost("Text", source_post_ids=["1"])], [], None)
    create_delivery(engine, delivery)

    with pytest.raises(ValueError):
        complete_delivery(engine, delivery)

    assert load_deliveries(engine, source.id) == [delivery]
    assert get_dest_post_id(engine, source.id, "1", dest.id) is None


def test_checkpoint_and_mirror_markers_commit_together(engine):
    source = create_account(engine, "twitter", "main", "source")
    dest = create_account(engine, "telegram", "main", "dest")
    delivery = Delivery(source.id, dest.id, "thread", [OutboundPost("Text", source_post_ids=["1"])], [], None)
    create_delivery(engine, delivery)

    def fail_mirror_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO mirrored_posts"):
            raise RuntimeError("Disk write failed")

    event.listen(engine, "before_cursor_execute", fail_mirror_insert)
    try:
        with pytest.raises(RuntimeError):
            record_delivery_part(engine, delivery, PublishResult("remote", "reply"))
    finally:
        event.remove(engine, "before_cursor_execute", fail_mirror_insert)

    assert delivery.dest_ids == []
    assert load_deliveries(engine, source.id) == [delivery]
    assert get_mirrored_post_ids(engine, dest.id) == set()
