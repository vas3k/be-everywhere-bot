from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Float,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

metadata = MetaData()

projects = Table(
    "projects", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("enabled", Boolean, nullable=False, server_default="1"),
    Column("min_age_minutes", Integer, nullable=False, server_default="20"),
    Column("overlap_hours", Integer, nullable=False, server_default="6"),
    Column("initial_lookback_hours", Integer, nullable=False, server_default="48"),
    Column("backfill_delay_seconds", Float, nullable=False, server_default="3"),
    Column("created_at", DateTime, nullable=False),
    Column("last_sync_started_at", DateTime),
    Column("last_sync_finished_at", DateTime),
    Column("last_synced_at", DateTime),
    Column("last_sync_count", Integer, nullable=False, server_default="0"),
    Column("last_sync_error_count", Integer, nullable=False, server_default="0"),
    CheckConstraint("length(trim(name)) > 0"),
    CheckConstraint("min_age_minutes >= 0 AND overlap_hours > 0 AND initial_lookback_hours > 0 AND backfill_delay_seconds >= 0"),
)

accounts = Table(
    "accounts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Integer, ForeignKey("projects.id"), nullable=False, server_default="1"),
    Column("network", String(32), nullable=False),
    Column("label", String(64), nullable=False),
    Column("remote_id", String(128), nullable=False),
    Column("sync_role", String(32), nullable=False, server_default="source_and_destination"),
    Column("created_at", DateTime, nullable=False),
    Column("deleted_at", DateTime),
    UniqueConstraint("project_id", "network", "label", name="uq_accounts_project_network_label"),
    CheckConstraint("sync_role IN ('source_and_destination', 'source_only', 'destination_only')"),
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


source_posts = Table(
    "source_posts", metadata,
    Column("account_id", Integer, ForeignKey("accounts.id"), primary_key=True),
    Column("post_id", String, primary_key=True),
    Column("payload", JSON, nullable=False),
)

sync_batches = Table(
    "sync_batches", metadata,
    Column("source_account_id", Integer, ForeignKey("accounts.id"), primary_key=True),
    Column("dest_account_id", Integer, ForeignKey("accounts.id"), primary_key=True),
    Column("conversation_id", String, primary_key=True),
    Column("outbounds", JSON, nullable=False),
    Column("dest_ids", JSON, nullable=False),
    Column("reply_ref", String),
)
