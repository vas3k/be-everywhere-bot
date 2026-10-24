"""Scope connections to projects without changing their IDs or dependent state."""

from datetime import datetime, timezone

from sqlalchemy import inspect
from sqlalchemy.dialects.sqlite import insert

from db.schema import projects

VERSION = "003_projects"


def upgrade(engine):
    with engine.connect() as connection:
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        connection.commit()
        try:
            # Explicit BEGIN keeps SQLite DDL and the copy in one rollbackable transaction.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            projects.create(connection, checkfirst=True)
            connection.execute(insert(projects).values(
                id=1, name="Default", created_at=datetime.now(timezone.utc),
            ).on_conflict_do_nothing(index_elements=["id"]))

            columns = {column["name"] for column in inspect(connection).get_columns("accounts")}
            if "project_id" not in columns:
                connection.exec_driver_sql("""
                    CREATE TABLE accounts_with_projects (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id INTEGER NOT NULL DEFAULT 1 REFERENCES projects(id),
                        network VARCHAR(32) NOT NULL,
                        label VARCHAR(64) NOT NULL,
                        remote_id VARCHAR(128) NOT NULL,
                        created_at DATETIME NOT NULL,
                        CONSTRAINT uq_accounts_project_network_label UNIQUE (project_id, network, label)
                    )
                """)
                connection.exec_driver_sql("""
                    INSERT INTO accounts_with_projects (id, project_id, network, label, remote_id, created_at)
                    SELECT id, 1, network, label, remote_id, created_at FROM accounts
                """)
                connection.exec_driver_sql("DROP TABLE accounts")
                connection.exec_driver_sql("ALTER TABLE accounts_with_projects RENAME TO accounts")
                connection.exec_driver_sql("""
                    UPDATE projects SET last_synced_at = (
                        SELECT MIN(last_synced_at) FROM account_sync_state
                    ) WHERE id = 1
                """)
            if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("Project migration found invalid foreign-key references")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql(f"PRAGMA foreign_keys = {int(foreign_keys)}")
            connection.commit()
