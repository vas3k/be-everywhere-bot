"""Add connection roles while preserving existing account IDs and sync state."""

from sqlalchemy import inspect

VERSION = "004_account_roles"


def upgrade(engine):
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            columns = {column["name"] for column in inspect(connection).get_columns("accounts")}
            if "sync_role" not in columns:
                connection.exec_driver_sql("""
                    ALTER TABLE accounts ADD COLUMN sync_role VARCHAR(32) NOT NULL
                    DEFAULT 'source_and_destination'
                    CHECK (sync_role IN ('source_and_destination', 'source_only', 'destination_only'))
                """)
            connection.exec_driver_sql("""
                UPDATE accounts SET sync_role = 'source_only' WHERE network IN ('rss', 'instagram')
            """)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
