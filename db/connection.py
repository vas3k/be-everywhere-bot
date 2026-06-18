from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from config import DATABASE_PATH
from db.schema import metadata


def get_engine(db_path: Path | None = None) -> Engine:
    path = db_path or DATABASE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    metadata.create_all(engine)
    return engine
