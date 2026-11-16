"""Promote the RSS-only opt-in to a general destination setting."""

from sqlalchemy import inspect

VERSION = "006_ai_summary"


def upgrade(engine):
    with engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("accounts")}
        if "ai_summary" not in columns:
            connection.exec_driver_sql("ALTER TABLE accounts ADD COLUMN ai_summary BOOLEAN NOT NULL DEFAULT 0")
        connection.exec_driver_sql("""
            UPDATE accounts SET ai_summary = 1
            WHERE deleted_at IS NULL AND sync_role != 'source_only'
            AND id IN (
                SELECT account_id FROM account_credentials
                WHERE key = 'rss_ai_summaries' AND value = 'true'
            )
        """)
        connection.exec_driver_sql("DELETE FROM account_credentials WHERE key = 'rss_ai_summaries'")
