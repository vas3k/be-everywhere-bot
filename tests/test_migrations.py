from datetime import datetime, timezone

import pytest
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, UniqueConstraint, create_engine, event, inspect, select

from db.connection import get_engine
from db.migrations.runner import run_migrations
from db.schema import (
    accounts, account_credentials, account_sync_state, mirrored_posts,
    projects, schema_migrations, source_posts, sync_batches, sync_mappings,
)


def test_upgrade_preserves_existing_accounts_credentials_and_sync_history(tmp_path):
    path = tmp_path / "previous-release.db"
    old = create_engine(f"sqlite:///{path}")
    old_accounts = Table(
        "accounts", MetaData(),
        Column("id", Integer, primary_key=True),
        Column("network", String(32), nullable=False),
        Column("label", String(64), nullable=False),
        Column("remote_id", String(128), nullable=False),
        Column("created_at", DateTime, nullable=False),
        UniqueConstraint("network", "label"),
    )
    old_tables = (old_accounts, account_credentials, account_sync_state, mirrored_posts, schema_migrations, sync_mappings, source_posts, sync_batches)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with old.begin() as connection:
        for table in old_tables:
            table.create(connection)
        connection.execute(old_accounts.insert(), [
            {"id": 11, "network": "twitter", "label": "personal", "remote_id": "alice", "created_at": now},
            {"id": 22, "network": "telegram", "label": "main", "remote_id": "channel", "created_at": now},
        ])
        connection.execute(account_credentials.insert(), [
            {"account_id": 11, "key": "bearer_token", "value": "stored-secret"},
            {"account_id": 22, "key": "update_offset", "value": "12345"},
        ])
        connection.execute(sync_mappings.insert().values(
            source_account_id=11, source_post_id="source-post", dest_account_id=22,
            dest_post_id="remote-post", synced_at=now,
        ))
        connection.execute(mirrored_posts.insert().values(account_id=22, post_id="remote-post", created_at=now))
        connection.execute(account_sync_state.insert().values(account_id=11, last_synced_at=now))
        connection.execute(schema_migrations.insert().values(version="001_mesh_accounts", applied_at=now))
        connection.execute(source_posts.insert().values(account_id=11, post_id="pending", payload={"text": "Saved content"}))
        connection.execute(sync_batches.insert().values(
            source_account_id=11, dest_account_id=22, conversation_id="pending",
            outbounds=[{"text": "Saved content", "media": [], "source_post_ids": ["pending"]}],
            dest_ids=["already-published"], reply_ref="already-published",
        ))
        before = {table.name: connection.execute(select(table)).all() for table in old_tables}
    old.dispose()

    upgraded = get_engine(path)
    run_migrations(upgraded)

    with upgraded.connect() as connection:
        for table in old_tables:
            rows = connection.execute(select(table)).all()
            if table is schema_migrations:
                assert before[table.name][0] in rows
                assert {row.version for row in rows} == {"001_mesh_accounts", "002_durable_sync", "003_projects", "004_account_roles", "005_removed_accounts"}
            else:
                assert rows == before[table.name]
        assert set(connection.execute(select(accounts.c.project_id)).scalars()) == {1}
        assert set(connection.execute(select(accounts.c.deleted_at)).scalars()) == {None}
        default = connection.execute(select(projects)).mappings().one()
        assert default["name"] == "Default"
        assert default["last_synced_at"] == now.replace(tzinfo=None)
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    upgraded.dispose()


def test_fresh_database_has_same_schema_and_migrations_are_repeatable(tmp_path):
    engine = get_engine(tmp_path / "fresh.db")
    run_migrations(engine)

    assert set(inspect(engine).get_table_names()) == {
        "accounts", "account_credentials", "account_sync_state", "mirrored_posts",
        "schema_migrations", "source_posts", "sync_batches", "sync_mappings", "projects",
    }
    with engine.connect() as connection:
        assert set(connection.execute(select(schema_migrations.c.version)).scalars()) == {
            "001_mesh_accounts", "002_durable_sync", "003_projects", "004_account_roles", "005_removed_accounts",
        }
    engine.dispose()


def test_additive_migration_recovers_if_tables_exist_but_version_was_not_recorded(tmp_path):
    engine = get_engine(tmp_path / "interrupted.db")
    with engine.begin() as connection:
        connection.execute(schema_migrations.delete().where(schema_migrations.c.version == "002_durable_sync"))
    run_migrations(engine)

    with engine.connect() as connection:
        assert connection.execute(select(schema_migrations.c.version).where(
            schema_migrations.c.version == "002_durable_sync",
        )).scalar_one() == "002_durable_sync"
    engine.dispose()


def test_project_migration_rolls_back_table_replacement_on_failure(tmp_path):
    from importlib import import_module
    migration = import_module("db.migrations.versions.003_projects")
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE accounts (id INTEGER PRIMARY KEY, network TEXT, label TEXT, remote_id TEXT, created_at DATETIME, UNIQUE(network, label))")
        connection.exec_driver_sql("INSERT INTO accounts VALUES (7, 'rss', 'blog', 'feed', '2026-01-01 00:00:00')")
        account_sync_state.create(connection)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")

    def interrupt_rename(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("ALTER TABLE accounts_with_projects"):
            raise RuntimeError("Simulated interruption")

    event.listen(engine, "before_cursor_execute", interrupt_rename)
    try:
        with pytest.raises(RuntimeError, match="Simulated interruption"):
            migration.upgrade(engine)
    finally:
        event.remove(engine, "before_cursor_execute", interrupt_rename)

    with engine.connect() as connection:
        assert "project_id" not in {column["name"] for column in inspect(connection).get_columns("accounts")}
        assert connection.exec_driver_sql("SELECT id, remote_id FROM accounts").all() == [(7, "feed")]
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    migration.upgrade(engine)
    migration.upgrade(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT id, project_id, remote_id FROM accounts").all() == [(7, 1, "feed")]
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_legacy_single_account_database_still_upgrades_into_default(tmp_path):
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE credentials (network TEXT, key TEXT, value TEXT)")
        connection.exec_driver_sql("INSERT INTO credentials VALUES ('twitter', 'user_id', 'alice')")
    engine.dispose()

    upgraded = get_engine(path)
    with upgraded.connect() as connection:
        account = connection.execute(select(accounts)).mappings().one()
        assert (account["network"], account["remote_id"], account["project_id"]) == ("twitter", "alice", 1)
        assert connection.execute(select(projects.c.name)).scalar_one() == "Default"
    upgraded.dispose()

    # Migration 001 reflects legacy tables into shared metadata; keep this fixture isolated.
    from db.schema import metadata
    metadata.remove(metadata.tables["credentials"])


def test_role_migration_preserves_accounts_and_assigns_network_defaults(tmp_path):
    from importlib import import_module
    migration = import_module("db.migrations.versions.004_account_roles")
    engine = create_engine(f"sqlite:///{tmp_path / 'roles.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE accounts (id INTEGER PRIMARY KEY, project_id INTEGER, network TEXT, label TEXT, remote_id TEXT)")
        connection.exec_driver_sql("INSERT INTO accounts VALUES (1, 1, 'rss', 'blog', 'feed'), (2, 1, 'instagram', 'photos', 'ig'), (3, 2, 'twitter', 'main', 'alice')")
        before = connection.exec_driver_sql("SELECT * FROM accounts").all()
    migration.upgrade(engine)
    with engine.begin() as connection:
        assert connection.exec_driver_sql("SELECT id, project_id, network, label, remote_id FROM accounts").all() == before
        assert connection.exec_driver_sql("SELECT sync_role FROM accounts ORDER BY id").scalars().all() == ["source_only", "source_only", "source_and_destination"]
        connection.exec_driver_sql("UPDATE accounts SET sync_role = 'destination_only' WHERE id = 3")
    migration.upgrade(engine)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT sync_role FROM accounts WHERE id = 3").scalar_one() == "destination_only"
    engine.dispose()
