from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection

from config import ACCOUNT_SYNC_ROLES, SOURCE_ONLY_NETWORKS
from db.schema import account_credentials, accounts


def write_credentials(connection: Connection, account_id: int, values: dict[str, str]) -> None:
    for key, value in values.items():
        statement = insert(account_credentials).values(account_id=account_id, key=key, value=value)
        connection.execute(statement.on_conflict_do_update(
            index_elements=["account_id", "key"],
            set_={"value": statement.excluded.value},
        ))


def remote_account_identity(network: str, remote_id: str, credentials: dict[str, str]) -> tuple[str, str, str]:
    scope = ""
    if network == "mastodon":
        scope = urlsplit(credentials.get("instance_url", "")).netloc.lower()
        remote_id = credentials.get("account_id", remote_id)
    if network == "telegram":
        remote_id = remote_id.lower()
    return network, scope, remote_id


def validate_account_role(
    connection: Connection, network: str, project_id: int, remote_id: str,
    sync_role: str, credentials: dict[str, str], *, allow_destination_only: bool = False,
) -> None:
    if sync_role not in ACCOUNT_SYNC_ROLES:
        raise ValueError(f"Unknown sync role: {sync_role}")
    if network in SOURCE_ONLY_NETWORKS and sync_role != "source_only":
        raise ValueError(f"{network} connections must be source_only")
    # Existing shared records can be repaired one at a time; remaining sources stay blocked.
    if allow_destination_only and sync_role == "destination_only":
        return

    identity = remote_account_identity(network, remote_id, credentials)
    peers = connection.execute(select(accounts).where(
        accounts.c.network == network, accounts.c.project_id != project_id, accounts.c.deleted_at.is_(None),
    )).mappings().all()
    for peer in peers:
        other_credentials = dict(connection.execute(select(
            account_credentials.c.key, account_credentials.c.value,
        ).where(account_credentials.c.account_id == peer["id"])).tuples().all())
        if remote_account_identity(network, peer["remote_id"], other_credentials) != identity:
            continue
        if sync_role != "destination_only" or peer["sync_role"] != "destination_only":
            raise ValueError(
                "An account shared across projects must be destination_only in every project. "
                "Change existing connections with --configure-account before adding another."
            )
