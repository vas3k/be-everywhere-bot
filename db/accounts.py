from dataclasses import dataclass
from uuid import uuid4
from datetime import datetime, timezone

from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from config import (
    NETWORK_BLUESKY,
    NETWORK_INSTAGRAM,
    NETWORK_LINKEDIN,
    NETWORK_MASTODON,
    NETWORK_RSS,
    NETWORK_TELEGRAM,
    NETWORK_THREADS,
    NETWORK_TWITTER,
    SOURCE_ONLY_NETWORKS,
)
from db.schema import account_credentials, accounts, source_posts, sync_batches
from db.helpers import validate_account_role, write_credentials
from db.projects import get_project


@dataclass(frozen=True)
class Account:
    id: int
    network: str
    label: str
    remote_id: str
    project_id: int = 1
    sync_role: str = "source_and_destination"


def account_from_row(row) -> Account:
    return Account(
        id=row.id,
        network=row.network,
        label=row.label,
        remote_id=row.remote_id,
        project_id=row.project_id,
        sync_role=row.sync_role,
    )


def list_accounts(engine: Engine, network: str | None = None, *, project_id: int | None = None, include_removed: bool = False) -> list[Account]:
    stmt = select(accounts).order_by(accounts.c.network, accounts.c.label)
    if not include_removed:
        stmt = stmt.where(accounts.c.deleted_at.is_(None))
    if project_id is not None:
        stmt = stmt.where(accounts.c.project_id == project_id)
    if network:
        stmt = stmt.where(accounts.c.network == network)
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return [account_from_row(row) for row in rows]


def get_account(engine: Engine, account_id: int) -> Account | None:
    stmt = select(accounts).where(accounts.c.id == account_id, accounts.c.deleted_at.is_(None))
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return account_from_row(row) if row else None


def find_account(engine: Engine, network: str, label: str, *, project_id: int = 1) -> Account | None:
    stmt = (
        select(accounts)
        .where(accounts.c.network == network)
        .where(accounts.c.label == label)
        .where(accounts.c.project_id == project_id)
        .where(accounts.c.deleted_at.is_(None))
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return account_from_row(row) if row else None


def create_account(
    engine: Engine,
    network: str,
    label: str,
    remote_id: str,
    *, project_id: int = 1, sync_role: str | None = None,
) -> Account:
    # Migration 001 creates Default connections before migration 003 seeds the project.
    if project_id != 1:
        get_project(engine, project_id)
    if sync_role is None:
        sync_role = "source_only" if network in SOURCE_ONLY_NETWORKS else "source_and_destination"
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        validate_account_role(conn, network, project_id, remote_id, sync_role, {})
        result = conn.execute(
            insert(accounts).values(
                project_id=project_id, sync_role=sync_role,
                network=network,
                label=label,
                remote_id=remote_id,
                created_at=now,
            )
        )
        account_id = result.inserted_primary_key[0]
    account = get_account(engine, account_id)
    assert account is not None
    return account


def get_credential(engine: Engine, account_id: int, key: str) -> str | None:
    stmt = (
        select(account_credentials.c.value)
        .where(account_credentials.c.account_id == account_id)
        .where(account_credentials.c.key == key)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return row[0] if row else None


def get_all_credentials(engine: Engine, account_id: int) -> dict[str, str]:
    stmt = select(account_credentials.c.key, account_credentials.c.value).where(
        account_credentials.c.account_id == account_id
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return {key: value for key, value in rows}


def set_credential(engine: Engine, account_id: int, key: str, value: str) -> None:
    with engine.begin() as connection:
        write_credentials(connection, account_id, {key: value})


def set_credentials(engine: Engine, account_id: int, values: dict[str, str]) -> None:
    with engine.begin() as connection:
        write_credentials(connection, account_id, values)


def save_account(
    engine: Engine, network: str, label: str, remote_id: str, credentials: dict[str, str],
    *, project_id: int = 1, sync_role: str | None = None,
) -> Account:
    get_project(engine, project_id)
    if not label or not remote_id:
        raise ValueError("Account label and remote identity are required")
    existing = find_account(engine, network, label, project_id=project_id)
    if sync_role is None:
        sync_role = existing.sync_role if existing else ("source_only" if network in SOURCE_ONLY_NETWORKS else "source_and_destination")
    with engine.begin() as connection:
        validate_account_role(connection, network, project_id, remote_id, sync_role, credentials)
        statement = sqlite_insert(accounts).values(
            project_id=project_id, network=network, label=label, remote_id=remote_id, sync_role=sync_role,
            created_at=datetime.now(timezone.utc),
        )
        account_id = connection.execute(statement.on_conflict_do_update(
            index_elements=["project_id", "network", "label"], set_={"remote_id": remote_id, "sync_role": sync_role},
        ).returning(accounts.c.id)).scalar_one()
        write_credentials(connection, account_id, credentials)
    return Account(account_id, network, label, remote_id, project_id, sync_role)


def set_account_role(engine: Engine, account_id: int, sync_role: str, *, label: str | None = None) -> Account:
    account = get_account(engine, account_id)
    if account is None:
        raise ValueError(f"Unknown account ID: {account_id}")
    label = account.label if label is None else label.strip()
    if not label:
        raise ValueError("Account label must not be empty")
    credentials = get_all_credentials(engine, account_id)
    with engine.begin() as connection:
        validate_account_role(
            connection, account.network, account.project_id, account.remote_id,
            sync_role, credentials, allow_destination_only=True,
        )
        connection.execute(update(accounts).where(accounts.c.id == account_id).values(sync_role=sync_role, label=label))
    return get_account(engine, account_id)


def update_remote_id(engine: Engine, account_id: int, remote_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(accounts)
            .where(accounts.c.id == account_id)
            .values(remote_id=remote_id)
        )


def account_display_name(account: Account, engine: Engine) -> str:
    creds = get_all_credentials(engine, account.id)
    if account.network == NETWORK_TWITTER:
        username = creds.get("username")
        if username:
            return f"@{username}"
    if account.network == NETWORK_TELEGRAM:
        channel = creds.get("channel_id")
        if channel:
            return channel
    if account.network == NETWORK_MASTODON:
        username = creds.get("username")
        instance = creds.get("instance_url", "")
        if username and instance:
            host = instance.removeprefix("https://").removeprefix("http://").rstrip("/")
            return f"@{username}@{host}"
    if account.network == NETWORK_THREADS:
        username = creds.get("username")
        if username:
            return f"@{username}"
    if account.network == NETWORK_BLUESKY:
        handle = creds.get("handle")
        if handle:
            return f"@{handle}"
    if account.network == NETWORK_RSS:
        feed_url = creds.get("feed_url")
        if feed_url:
            return feed_url
    if account.network == NETWORK_INSTAGRAM:
        username = creds.get("username")
        if username:
            return f"@{username}"
    if account.network == NETWORK_LINKEDIN:
        display_name = creds.get("display_name")
        if display_name:
            return display_name
    return f"{account.network}:{account.label}"


def remove_account(engine: Engine, account_id: int) -> None:
    account = get_account(engine, account_id)
    if account is None:
        raise ValueError(f"Unknown account ID: {account_id}")
    with engine.begin() as connection:
        connection.execute(delete(sync_batches).where(or_(
            sync_batches.c.source_account_id == account_id,
            sync_batches.c.dest_account_id == account_id,
        )))
        connection.execute(delete(source_posts).where(source_posts.c.account_id == account_id))
        # Mastodon needs its instance and numeric identity to recognize old mirrored posts.
        retained_keys = ("instance_url", "account_id") if account.network == "mastodon" else ()
        connection.execute(delete(account_credentials).where(
            account_credentials.c.account_id == account_id,
            account_credentials.c.key.not_in(retained_keys),
        ))
        connection.execute(update(accounts).where(accounts.c.id == account_id).values(
            deleted_at=datetime.now(timezone.utc), label=f"removed-{account_id}-{uuid4().hex}",
        ))
