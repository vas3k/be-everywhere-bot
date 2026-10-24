from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from db.accounts import remove_account, save_account
from db.history import recent_history
from db.projects import create_project
from db.schema import accounts, projects, source_posts
from db.sync_state import mark_synced


def test_history_orders_events_by_date_and_keeps_deliveries_without_cached_text(engine):
    project = create_project(engine, "Work")
    source = save_account(engine, "twitter", "personal", "alice", {}, project_id=project.id)
    destination = save_account(engine, "telegram", "channel", "123", {}, project_id=project.id)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=2)
    latest = now + timedelta(seconds=2)
    with engine.begin() as connection:
        connection.execute(update(projects).values(created_at=old))
        connection.execute(update(accounts).values(created_at=old))
        connection.execute(source_posts.insert().values(account_id=source.id, post_id="new", payload={"text": "Hello\nworld"}))
        connection.execute(update(projects).where(projects.c.id == project.id).values(
            last_sync_finished_at=now + timedelta(seconds=1), last_sync_count=2, last_sync_error_count=1,
        ))
    mark_synced(engine, source.id, "new", destination.id, "copy-new", synced_at=latest)
    mark_synced(engine, source.id, "old", destination.id, "copy-old", synced_at=now)

    history = recent_history(engine, limit=3)
    assert [entry.kind for entry in history] == ["SYNCED", "ERRORS", "SYNCED"]
    assert all(entry.project == "Work" for entry in history)
    assert history[0].summary == "twitter/personal → telegram/channel"
    assert "Hello world" in history[0].detail
    assert history[2].detail == "Post old → copy-old"
    assert recent_history(engine, limit=0) == []


def test_history_preserves_removed_account_deliveries_without_credentials(engine):
    source = save_account(engine, "twitter", "personal", "alice", {"bearer_token": "secret-token"})
    destination = save_account(engine, "telegram", "channel", "123", {})
    mark_synced(engine, source.id, "original", destination.id, "copy")
    remove_account(engine, source.id)

    history = recent_history(engine)
    assert any(entry.kind == "REMOVED" for entry in history)
    delivery = next(entry for entry in history if entry.kind == "SYNCED")
    assert f"removed #{source.id}" in delivery.summary
    assert delivery.detail == "Post original → copy"
    assert "secret-token" not in repr(history)
