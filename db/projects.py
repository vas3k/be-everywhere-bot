from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from config import BACKFILL_POST_DELAY_SECONDS, POST_MIN_AGE_MINUTES, WATCH_INITIAL_LOOKBACK_HOURS, WATCH_OVERLAP_HOURS
from db.schema import projects


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    enabled: bool
    min_age_minutes: int
    overlap_hours: int
    initial_lookback_hours: int
    backfill_delay_seconds: float
    created_at: datetime
    last_sync_started_at: datetime | None
    last_sync_finished_at: datetime | None
    last_synced_at: datetime | None
    last_sync_count: int
    last_sync_error_count: int


def project_from_row(row) -> Project:
    values = dict(row)
    for key in ("created_at", "last_sync_started_at", "last_sync_finished_at", "last_synced_at"):
        if values[key] is not None:
            values[key] = values[key].replace(tzinfo=timezone.utc)
    return Project(**values)


def list_projects(engine: Engine) -> list[Project]:
    with engine.connect() as connection:
        rows = connection.execute(select(projects).order_by(projects.c.id)).mappings().all()
    return [project_from_row(row) for row in rows]


def get_project(engine: Engine, project_id: int) -> Project:
    with engine.connect() as connection:
        row = connection.execute(select(projects).where(projects.c.id == project_id)).mappings().first()
    if row is None:
        raise ValueError(f"Unknown project ID: {project_id}")
    return project_from_row(row)


def find_project(engine: Engine, name: str) -> Project:
    with engine.connect() as connection:
        row = connection.execute(select(projects).where(projects.c.name == name)).mappings().first()
    if row is None:
        raise ValueError(f"Unknown project: {name}. Create it with --create-project.")
    return project_from_row(row)


def create_project(engine: Engine, name: str) -> Project:
    name = name.strip()
    if not name:
        raise ValueError("Project name must not be empty")
    with engine.begin() as connection:
        project_id = connection.execute(insert(projects).values(
            name=name, created_at=datetime.now(timezone.utc),
            min_age_minutes=POST_MIN_AGE_MINUTES,
            overlap_hours=WATCH_OVERLAP_HOURS,
            initial_lookback_hours=WATCH_INITIAL_LOOKBACK_HOURS,
            backfill_delay_seconds=BACKFILL_POST_DELAY_SECONDS,
        ).returning(projects.c.id)).scalar_one()
    return get_project(engine, project_id)


def configure_project(
    engine: Engine, project_id: int, *, enabled: bool | None = None,
    name: str | None = None, min_age_minutes: int | None = None, overlap_hours: int | None = None,
    initial_lookback_hours: int | None = None, backfill_delay_seconds: float | None = None,
) -> Project:
    get_project(engine, project_id)
    values = {
        "enabled": enabled, "min_age_minutes": min_age_minutes,
        "overlap_hours": overlap_hours, "initial_lookback_hours": initial_lookback_hours,
        "backfill_delay_seconds": backfill_delay_seconds,
    }
    values = {key: value for key, value in values.items() if value is not None}
    for key, value in values.items():
        if key != "enabled" and (not isfinite(value) or value < 0 or (key in ("overlap_hours", "initial_lookback_hours") and value == 0)):
            raise ValueError(f"Invalid project setting: {key}={value}")
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("Project name must not be empty")
        if project_id == 1 and name != "Default":
            raise ValueError("The Default project cannot be renamed")
        values["name"] = name
    if values:
        with engine.begin() as connection:
            connection.execute(update(projects).where(projects.c.id == project_id).values(**values))
    return get_project(engine, project_id)
