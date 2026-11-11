from pathlib import Path
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from config import DATABASE_PATH
from db.migrations.runner import run_migrations


def get_engine(db_path: Path | None = None, *, read_only: bool = False) -> Engine:
    path = db_path or DATABASE_PATH
    if read_only:
        if not path.is_file():
            raise ValueError("Dry run requires an existing database; initialize it first")

        def connect_read_only():
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            connection.execute("PRAGMA query_only = ON")
            return connection

        return create_engine("sqlite://", creator=connect_read_only)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    return engine
