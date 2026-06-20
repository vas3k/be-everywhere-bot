"""Migrate legacy single-account schema to mesh sync with accounts."""

from datetime import datetime, timezone

from sqlalchemy import Table, insert, select
from sqlalchemy.engine import Engine

from db.accounts import create_account, set_credentials
from db.schema import metadata
from db.sync_state import record_mirrored_post

VERSION = "001_mesh_accounts"


def _legacy_table(engine: Engine, name: str) -> Table | None:
    inspector = __import__("sqlalchemy").inspect(engine)
    if not inspector.has_table(name):
        return None
    return Table(name, metadata, autoload_with=engine)


def upgrade(engine: Engine) -> None:
    legacy_credentials = _legacy_table(engine, "credentials")
    legacy_posted = _legacy_table(engine, "posted")
    legacy_sync_state = _legacy_table(engine, "sync_state")

    if legacy_credentials is None:
        return

    from db.schema import accounts

    with engine.connect() as conn:
        existing = conn.execute(select(accounts.c.id).limit(1)).first()
    if existing is not None:
        return

    network_accounts: dict[str, int] = {}

    with engine.connect() as conn:
        rows = conn.execute(
            select(legacy_credentials.c.network, legacy_credentials.c.key, legacy_credentials.c.value)
        ).all()

    creds_by_network: dict[str, dict[str, str]] = {}
    for network, key, value in rows:
        creds_by_network.setdefault(network, {})[key] = value

    for network, creds in creds_by_network.items():
        remote_id = (
            creds.get("user_id")
            or creds.get("channel_id")
            or creds.get("username")
            or creds.get("instance_url")
            or network
        )
        account = create_account(engine, network, "default", str(remote_id))
        set_credentials(engine, account.id, creds)
        network_accounts[network] = account.id

    if legacy_posted is not None:
        with engine.connect() as conn:
            posted_rows = conn.execute(
                select(
                    legacy_posted.c.source_network,
                    legacy_posted.c.source_post_id,
                    legacy_posted.c.destination_network,
                    legacy_posted.c.destination_post_id,
                    legacy_posted.c.posted_at,
                )
            ).all()

        from db.schema import sync_mappings

        for (
            source_network,
            source_post_id,
            dest_network,
            dest_post_id,
            posted_at,
        ) in posted_rows:
            source_account_id = network_accounts.get(source_network)
            dest_account_id = network_accounts.get(dest_network)
            if source_account_id is None or dest_account_id is None:
                continue
            with engine.begin() as conn:
                conn.execute(
                    insert(sync_mappings).values(
                        source_account_id=source_account_id,
                        source_post_id=source_post_id,
                        dest_account_id=dest_account_id,
                        dest_post_id=dest_post_id,
                        synced_at=posted_at or datetime.now(timezone.utc),
                    )
                )
            record_mirrored_post(
                engine,
                dest_account_id,
                dest_post_id,
                created_at=posted_at,
            )

    if legacy_sync_state is not None:
        with engine.connect() as conn:
            state_rows = conn.execute(
                select(
                    legacy_sync_state.c.source_network,
                    legacy_sync_state.c.last_synced_at,
                )
            ).all()

        from db.schema import account_sync_state

        seen_accounts: set[int] = set()
        for source_network, last_synced_at in state_rows:
            account_id = network_accounts.get(source_network)
            if account_id is None or account_id in seen_accounts:
                continue
            seen_accounts.add(account_id)
            with engine.begin() as conn:
                conn.execute(
                    insert(account_sync_state).values(
                        account_id=account_id,
                        last_synced_at=last_synced_at,
                    )
                )

    for table_name in ("posted", "sync_state", "credentials"):
        legacy = _legacy_table(engine, table_name)
        if legacy is not None:
            legacy.drop(engine, checkfirst=True)
