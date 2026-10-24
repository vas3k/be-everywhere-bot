"""Keep removed identities and mirror history without retaining login credentials."""

from sqlalchemy import inspect

VERSION = "005_removed_accounts"


def upgrade(engine):
    with engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("accounts")}
        if "deleted_at" not in columns:
            connection.exec_driver_sql("ALTER TABLE accounts ADD COLUMN deleted_at DATETIME")
