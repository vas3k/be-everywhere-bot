from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from db.schema import credentials


def get_credential(engine: Engine, network: str, key: str) -> str | None:
    stmt = (
        select(credentials.c.value)
        .where(credentials.c.network == network)
        .where(credentials.c.key == key)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return row[0] if row else None


def get_all_credentials(engine: Engine, network: str) -> dict[str, str]:
    stmt = select(credentials.c.key, credentials.c.value).where(
        credentials.c.network == network
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    return {key: value for key, value in rows}


def set_credential(engine: Engine, network: str, key: str, value: str) -> None:
    stmt = delete(credentials).where(
        credentials.c.network == network,
        credentials.c.key == key,
    )
    with engine.begin() as conn:
        conn.execute(stmt)
        conn.execute(
            insert(credentials).values(network=network, key=key, value=value)
        )


def set_credentials(engine: Engine, network: str, values: dict[str, str]) -> None:
    for key, value in values.items():
        set_credential(engine, network, key, value)
