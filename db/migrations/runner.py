"""Apply pending database migrations."""

import importlib
import logging
import pkgutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, insert, select
from sqlalchemy.engine import Engine

from db.schema import metadata, schema_migrations

logger = logging.getLogger(__name__)


def _applied_versions(engine: Engine) -> set[str]:
    if not inspect(engine).has_table("schema_migrations"):
        return set()
    with engine.connect() as conn:
        rows = conn.execute(select(schema_migrations.c.version)).all()
    return {row[0] for row in rows}


def _record_migration(engine: Engine, version: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(schema_migrations).values(
                version=version,
                applied_at=datetime.now(timezone.utc),
            )
        )


def run_migrations(engine: Engine) -> None:
    """Create tables and run any pending migrations."""
    metadata.create_all(engine)

    applied = _applied_versions(engine)
    migrations_pkg = "db.migrations.versions"
    versions_path = Path(__file__).resolve().parent / "versions"

    migration_modules: list[tuple[str, object]] = []
    for module_info in pkgutil.iter_modules([str(versions_path)]):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{migrations_pkg}.{module_info.name}")
        version = getattr(module, "VERSION", module_info.name)
        migration_modules.append((version, module))

    migration_modules.sort(key=lambda item: item[0])

    for version, module in migration_modules:
        if version in applied:
            continue
        logger.info("Applying migration %s", version)
        module.upgrade(engine)
        _record_migration(engine, version)
        logger.info("Migration %s applied", version)
