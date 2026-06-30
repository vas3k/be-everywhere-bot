from db.accounts import create_account
from db.sync_state import (
    get_mirrored_post_ids,
    is_synced,
    mark_synced,
    record_mirrored_post,
)
from config import NETWORK_TWITTER


def test_mark_synced_is_idempotent(engine):
    src = create_account(engine, NETWORK_TWITTER, "src", "1")
    dest = create_account(engine, NETWORK_TWITTER, "dest", "2")

    assert mark_synced(engine, src.id, "post-1", dest.id, "dest-1") is True
    assert mark_synced(engine, src.id, "post-1", dest.id, "dest-1") is False
    assert is_synced(engine, src.id, "post-1", dest.id) is True


def test_record_mirrored_post(engine):
    account = create_account(engine, NETWORK_TWITTER, "main", "1")
    record_mirrored_post(engine, account.id, "mirrored-99")
    record_mirrored_post(engine, account.id, "mirrored-99")
    assert get_mirrored_post_ids(engine, account.id) == {"mirrored-99"}
