import hashlib
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from apis.types import MediaItem, OutboundPost
from db.accounts import create_account
from db.connection import get_engine
from db.deliveries import Delivery, create_delivery, load_deliveries
from db.posts import save_posts
from db.schema import metadata
from db.sync_state import mark_synced, record_mirrored_post
from main import async_main, build_parser
from sync.engine import run_sync


async def test_dry_run_plans_cached_thread_and_pending_parts_without_network_or_changes(
    engine, post_factory, tmp_path, monkeypatch, caplog
):
    from apis.registry import NETWORK_APIS

    source = create_account(engine, "twitter", "main", "source")
    destination = create_account(engine, "telegram", "main", "destination")
    other = create_account(engine, "mastodon", "other", "other")
    posts = [post_factory("1", text="Read https://t.co/short", media=[MediaItem("https://media.test/a.jpg", "photo")]),
             post_factory("2", text="Tail", conversation_id="1", in_reply_to_id="1")]
    with engine.begin() as connection:
        save_posts(connection, source.id, posts)
    delivery = Delivery(source.id, destination.id, "1", [
        OutboundPost("Root", source_post_ids=["1"]), OutboundPost("Tail", source_post_ids=["2"]),
    ], ["already-posted"], "already-posted")
    create_delivery(engine, delivery)
    mark_synced(engine, source.id, "1", other.id, "existing")
    record_mirrored_post(engine, destination.id, "already-posted")
    forbidden = AsyncMock(side_effect=AssertionError("Dry run must not call an API"))
    for api in NETWORK_APIS.values():
        for name in ("fetch_posts", "publish_outbound", "download_media"):
            monkeypatch.setattr(api, name, forbidden)
    monkeypatch.setattr("sync.engine.unwrap_posts_text", forbidden)
    monkeypatch.setattr("apis.bluesky.resolve_reply_target", forbidden)
    path = tmp_path / "test.db"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with engine.connect() as connection:
        before = {table.name: connection.execute(select(table)).all() for table in metadata.sorted_tables}

    readonly = get_engine(path, read_only=True)
    try:
        with caplog.at_level("INFO"):
            assert await run_sync(readonly, dry_run=True, enforce_min_age=False) == 3
    finally:
        readonly.dispose()

    forbidden.assert_not_awaited()
    assert "1 unsent part(s), 1 already checkpointed" in caplog.text
    assert "source IDs=2" in caplog.text
    assert load_deliveries(engine, source.id) == [delivery]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    with engine.connect() as connection:
        for table in metadata.sorted_tables:
            assert connection.execute(select(table)).all() == before[table.name]


def test_read_only_connection_does_not_create_or_migrate_databases(engine, tmp_path, monkeypatch):
    migrations = AsyncMock(side_effect=AssertionError("Must not migrate"))
    monkeypatch.setattr("db.connection.run_migrations", migrations)
    readonly = get_engine(tmp_path / "test.db", read_only=True)
    try:
        with readonly.connect() as connection:
            with pytest.raises(Exception, match="readonly|read.only"):
                connection.exec_driver_sql("CREATE TABLE forbidden (id INTEGER)")
    finally:
        readonly.dispose()
    migrations.assert_not_called()
    missing = tmp_path / "missing" / "empty.db"
    with pytest.raises(ValueError, match="existing database"):
        get_engine(missing, read_only=True)
    assert not missing.parent.exists()


async def test_cli_dry_run_uses_read_only_database_without_a_terminal(engine, monkeypatch):
    calls = []

    def open_database(*, read_only):
        calls.append(read_only)
        return engine

    monkeypatch.setattr("main.get_engine", open_database)
    monkeypatch.setattr("main.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("main.sys.stdout.isatty", lambda: False)
    await async_main(build_parser().parse_args(["--dry-run", "--project", "Default"]))
    assert calls == [True]


@pytest.mark.parametrize("action", [["--watch"], ["--auth", "twitter"], ["--migrate"], ["--create-project", "Other"]])
async def test_cli_dry_run_refuses_commands_that_write_or_run_continuously(monkeypatch, action):
    monkeypatch.setattr("main.get_engine", lambda: pytest.fail("Must reject before opening database"))
    with pytest.raises(ValueError, match="--dry-run"):
        await async_main(build_parser().parse_args(["--dry-run", *action]))
