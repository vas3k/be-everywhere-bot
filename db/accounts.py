from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from db.schema import account_credentials, accounts


@dataclass(frozen=True)
class Account:
    id: int
    network: str
    label: str
    remote_id: str


def _row_to_account(row) -> Account:
    return Account(
        id=row.id,
        network=row.network,
        label=row.label,
        remote_id=row.remote_id,
    )


def list_accounts(engine: Engine, network: str | None = None) -> list[Account]:
    stmt = select(accounts).order_by(accounts.c.network, accounts.c.label)
    if network:
        stmt = stmt.where(accounts.c.network == network)
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return [_row_to_account(row) for row in rows]


def get_account(engine: Engine, account_id: int) -> Account | None:
    stmt = select(accounts).where(accounts.c.id == account_id)
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return _row_to_account(row) if row else None


def find_account(engine: Engine, network: str, label: str) -> Account | None:
    stmt = (
        select(accounts)
        .where(accounts.c.network == network)
        .where(accounts.c.label == label)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return _row_to_account(row) if row else None


def create_account(
    engine: Engine,
    network: str,
    label: str,
    remote_id: str,
) -> Account:
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        result = conn.execute(
            insert(accounts).values(
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
    existing = get_credential(engine, account_id, key)
    with engine.begin() as conn:
        if existing is None:
            conn.execute(
                insert(account_credentials).values(
                    account_id=account_id, key=key, value=value
                )
            )
        else:
            conn.execute(
                update(account_credentials)
                .where(account_credentials.c.account_id == account_id)
                .where(account_credentials.c.key == key)
                .values(value=value)
            )


def set_credentials(engine: Engine, account_id: int, values: dict[str, str]) -> None:
    for key, value in values.items():
        set_credential(engine, account_id, key, value)


def update_remote_id(engine: Engine, account_id: int, remote_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(accounts)
            .where(accounts.c.id == account_id)
            .values(remote_id=remote_id)
        )


def delete_account_credentials(engine: Engine, account_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            delete(account_credentials).where(
                account_credentials.c.account_id == account_id
            )
        )


def account_display_name(account: Account, engine: Engine) -> str:
    creds = get_all_credentials(engine, account.id)
    if account.network == "twitter":
        username = creds.get("username")
        if username:
            return f"@{username}"
    if account.network == "telegram":
        channel = creds.get("channel_id")
        if channel:
            return channel
    if account.network == "mastodon":
        username = creds.get("username")
        instance = creds.get("instance_url", "")
        if username and instance:
            host = instance.removeprefix("https://").removeprefix("http://").rstrip("/")
            return f"@{username}@{host}"
    if account.network == "threads":
        username = creds.get("username")
        if username:
            return f"@{username}"
    if account.network == "bluesky":
        handle = creds.get("handle")
        if handle:
            return f"@{handle}"
    if account.network == "rss":
        feed_url = creds.get("feed_url")
        if feed_url:
            return feed_url
    return f"{account.network}:{account.label}"
