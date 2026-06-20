from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()

accounts = Table(
    "accounts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("network", String(32), nullable=False),
    Column("label", String(64), nullable=False),
    Column("remote_id", String(128), nullable=False),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("network", "label", name="uq_accounts_network_label"),
)

account_credentials = Table(
    "account_credentials",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("key", String(64), nullable=False),
    Column("value", String(4096), nullable=False),
    UniqueConstraint("account_id", "key", name="uq_account_credentials_account_key"),
)

sync_mappings = Table(
    "sync_mappings",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("source_account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("source_post_id", String(64), nullable=False),
    Column("dest_account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("dest_post_id", String(64), nullable=False),
    Column("synced_at", DateTime, nullable=False),
    UniqueConstraint(
        "source_account_id",
        "source_post_id",
        "dest_account_id",
        name="uq_sync_mappings_source_dest",
    ),
)

mirrored_posts = Table(
    "mirrored_posts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("post_id", String(64), nullable=False),
    Column("created_at", DateTime, nullable=False),
    UniqueConstraint("account_id", "post_id", name="uq_mirrored_posts_account_post"),
)

account_sync_state = Table(
    "account_sync_state",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", Integer, ForeignKey("accounts.id"), nullable=False),
    Column("last_synced_at", DateTime),
    UniqueConstraint("account_id", name="uq_account_sync_state_account"),
)

schema_migrations = Table(
    "schema_migrations",
    metadata,
    Column("version", String(32), primary_key=True),
    Column("applied_at", DateTime, nullable=False),
)
