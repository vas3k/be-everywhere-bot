from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()

credentials = Table(
    "credentials",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("network", String(32), nullable=False),
    Column("key", String(64), nullable=False),
    Column("value", String(4096), nullable=False),
    UniqueConstraint("network", "key", name="uq_credentials_network_key"),
)

posted = Table(
    "posted",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_network", String(32), nullable=False),
    Column("source_post_id", String(64), nullable=False),
    Column("destination_network", String(32), nullable=False),
    Column("destination_post_id", String(64), nullable=False),
    Column("posted_at", DateTime, nullable=False),
    UniqueConstraint(
        "source_network",
        "source_post_id",
        "destination_network",
        name="uq_posted_source_dest",
    ),
)

sync_state = Table(
    "sync_state",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_network", String(32), nullable=False),
    Column("destination_network", String(32), nullable=False),
    Column("last_synced_at", DateTime),
    UniqueConstraint(
        "source_network",
        "destination_network",
        name="uq_sync_state_pair",
    ),
)
