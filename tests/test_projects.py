from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from db.accounts import create_account, find_account, get_account, get_all_credentials, list_accounts, save_account, set_credentials
from db.projects import configure_project, create_project, find_project, get_project, list_projects
from db.sync_state import get_mirrored_post_ids, record_mirrored_post
from main import async_main, build_parser


def test_connections_are_unique_per_project_and_keep_credentials_separate(engine):
    project = create_project(engine, "Work")
    first = save_account(engine, "twitter", "main", "alice", {"token": "personal"}, sync_role="destination_only")
    second = save_account(engine, "twitter", "main", "alice", {"token": "work"}, project_id=project.id, sync_role="destination_only")
    colleague = create_account(engine, "twitter", "colleague", "bob", project_id=project.id)

    assert first.id != second.id
    assert find_account(engine, "twitter", "main").id == first.id
    assert find_account(engine, "twitter", "main", project_id=project.id).id == second.id
    assert {account.id for account in list_accounts(engine, project_id=project.id)} == {second.id, colleague.id}
    assert get_all_credentials(engine, first.id) == {"token": "personal"}
    assert get_all_credentials(engine, second.id) == {"token": "work"}
    with pytest.raises(IntegrityError):
        create_account(engine, "twitter", "main", "someone-else", project_id=project.id)
    with pytest.raises(ValueError):
        save_account(engine, "twitter", "main", "alice", {}, project_id=999)


@pytest.mark.parametrize("settings", [
    {"min_age_minutes": -1}, {"overlap_hours": 0},
    {"initial_lookback_hours": -1}, {"backfill_delay_seconds": -1},
    {"backfill_delay_seconds": float("nan")}, {"backfill_delay_seconds": float("inf")},
])
def test_invalid_settings_do_not_change_project(engine, settings):
    before = get_project(engine, 1)
    with pytest.raises(ValueError):
        configure_project(engine, 1, **settings)
    assert get_project(engine, 1) == before


def test_project_names_are_required_unique_and_settings_are_persistent(engine):
    from db.connection import get_engine
    from pathlib import Path
    with pytest.raises(ValueError):
        create_project(engine, "  ")
    with pytest.raises(IntegrityError):
        create_project(engine, "Default")
    project = create_project(engine, "Work")
    configured = configure_project(engine, project.id, enabled=False, min_age_minutes=45)
    reopened = get_engine(Path(engine.url.database))
    try:
        assert find_project(reopened, "Work") == configured
        assert [project.name for project in list_projects(reopened)] == ["Default", "Work"]
    finally:
        reopened.dispose()


@pytest.mark.parametrize("same_instance", [True, False])
@pytest.mark.parametrize("legacy_identity", [True, False])
def test_mastodon_mirror_identity_includes_instance(engine, same_instance, legacy_identity):
    project = create_project(engine, "Work")
    first = create_account(engine, "mastodon", "main", "alice" if legacy_identity else "123", sync_role="destination_only")
    second = create_account(engine, "mastodon", "main", "123", project_id=project.id, sync_role="destination_only")
    set_credentials(engine, first.id, {"instance_url": "https://first.test", "account_id": "123"})
    set_credentials(engine, second.id, {"instance_url": "https://FIRST.test/" if same_instance else "https://second.test", "account_id": "123"})
    record_mirrored_post(engine, first.id, "same-post-id")

    assert get_mirrored_post_ids(engine, second.id) == ({"same-post-id"} if same_instance else set())


def test_cli_creates_and_configures_projects_without_authentication(engine, monkeypatch):
    import asyncio
    monkeypatch.setattr("main.get_engine", lambda: engine)
    asyncio.run(async_main(build_parser().parse_args(["--create-project", "Work"])))
    asyncio.run(async_main(build_parser().parse_args([
        "--project", "Work", "--configure-project", "--no-enabled", "--min-age-minutes", "45",
        "--overlap-hours", "3", "--initial-lookback-hours", "72", "--backfill-delay-seconds", "1.5",
    ])))

    project = find_project(engine, "Work")
    assert not project.enabled
    assert (project.min_age_minutes, project.overlap_hours, project.initial_lookback_hours, project.backfill_delay_seconds) == (45, 3, 72, 1.5)
    assert list_accounts(engine) == []


@pytest.mark.parametrize("project_name", [None, "Work"])
async def test_cli_authentication_targets_explicit_project_or_default(engine, monkeypatch, project_name):
    project = create_project(engine, "Work")
    monkeypatch.setattr("main.get_engine", lambda: engine)
    authenticate = AsyncMock()
    monkeypatch.setattr("apis.twitter.authenticate", authenticate)
    arguments = ["--auth", "twitter", "--label", "main"]
    if project_name:
        arguments += ["--project", project_name]

    await async_main(build_parser().parse_args(arguments))

    authenticate.assert_awaited_once_with(engine, label="main", project_id=project.id if project_name else 1, sync_role=None)


@pytest.mark.parametrize("project_name", [None, "Work"])
async def test_cli_backfill_targets_explicit_project_or_all(engine, monkeypatch, project_name):
    project = create_project(engine, "Work")
    monkeypatch.setattr("main.get_engine", lambda: engine)
    sync = AsyncMock(return_value=0)
    monkeypatch.setattr("main.run_sync", sync)
    arguments = ["--since", "2026-01-01"]
    if project_name:
        arguments += ["--project", project_name]

    await async_main(build_parser().parse_args(arguments))

    assert sync.await_args.kwargs["project_id"] == (project.id if project_name else None)
    assert sync.await_args.kwargs["enforce_min_age"] is False


async def test_unknown_project_does_not_fall_back_to_default_or_run_sync(engine, monkeypatch):
    monkeypatch.setattr("main.get_engine", lambda: engine)
    sync = AsyncMock()
    monkeypatch.setattr("main.run_sync", sync)
    with pytest.raises(ValueError, match="Unknown project"):
        await async_main(build_parser().parse_args(["--project", "typo", "--since", "2026-01-01"]))
    sync.assert_not_awaited()
