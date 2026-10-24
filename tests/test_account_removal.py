from sqlalchemy import select

from db.accounts import get_account, get_all_credentials, list_accounts, remove_account, save_account
from db.projects import create_project
from db.schema import source_posts, sync_batches
from db.sync_state import get_mirrored_post_ids, is_synced, mark_synced, record_mirrored_post


def test_removal_erases_credentials_and_pending_work_but_retains_loop_protection(engine):
    source = save_account(engine, "twitter", "personal", "alice", {"bearer_token": "secret"})
    destination = save_account(engine, "telegram", "channel", "123", {"bot_token": "other-secret"})
    mark_synced(engine, source.id, "original", destination.id, "copy")
    record_mirrored_post(engine, source.id, "old-bot-post")
    with engine.begin() as connection:
        connection.execute(source_posts.insert().values(account_id=source.id, post_id="pending", payload={}))
        connection.execute(sync_batches.insert(), [
            dict(source_account_id=source.id, dest_account_id=destination.id, conversation_id="outgoing", outbounds=[], dest_ids=[]),
            dict(source_account_id=destination.id, dest_account_id=source.id, conversation_id="incoming", outbounds=[], dest_ids=[]),
        ])

    remove_account(engine, source.id)

    assert get_account(engine, source.id) is None
    assert list_accounts(engine) == [destination]
    assert get_all_credentials(engine, source.id) == {}
    assert get_all_credentials(engine, destination.id) == {"bot_token": "other-secret"}
    assert is_synced(engine, source.id, "original", destination.id)
    with engine.connect() as connection:
        assert connection.execute(select(source_posts)).all() == []
        assert connection.execute(select(sync_batches)).all() == []
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []

    project = create_project(engine, "New project")
    replacement = save_account(engine, "twitter", "personal", "alice", {}, project_id=project.id)
    assert replacement.id != source.id
    assert get_mirrored_post_ids(engine, replacement.id) == {"old-bot-post"}


def test_removed_mastodon_identity_remains_scoped_to_its_instance(engine):
    account = save_account(engine, "mastodon", "main", "123", {
        "instance_url": "https://one.test", "account_id": "123", "access_token": "secret",
    })
    record_mirrored_post(engine, account.id, "old-post")
    remove_account(engine, account.id)
    assert get_all_credentials(engine, account.id) == {"instance_url": "https://one.test", "account_id": "123"}

    replacement = save_account(engine, "mastodon", "main", "123", {"instance_url": "https://one.test", "account_id": "123"})
    unrelated = save_account(engine, "mastodon", "other", "123", {"instance_url": "https://two.test", "account_id": "123"})
    assert get_mirrored_post_ids(engine, replacement.id) == {"old-post"}
    assert get_mirrored_post_ids(engine, unrelated.id) == set()
