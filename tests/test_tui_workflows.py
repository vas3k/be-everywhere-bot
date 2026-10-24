from unittest.mock import AsyncMock

import pytest
from textual.widgets import Button, Input

from db.accounts import get_account, save_account
from tui.app import BotApp
from tui.forms import Editor
from tui.task_log import TaskLog


@pytest.mark.parametrize("project_id", [None, 1])
async def test_tui_starts_without_syncing(engine, monkeypatch, project_id):
    sync = AsyncMock()
    monkeypatch.setattr("tui.app.run_sync", sync)
    save_account(engine, "twitter", "personal", "alice", {"username": "alice"})
    app = BotApp(engine, project_id=project_id)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        if project_id is None:
            await pilot.click("#project-1")
            await pilot.pause()
        assert app.project_id == 1
        assert app.selected_account_id is not None
        sync.assert_not_awaited()


async def test_account_removal_requires_confirmation(engine):
    account = save_account(engine, "twitter", "personal", "alice", {"username": "alice"})
    app = BotApp(engine, project_id=1)
    async with app.run_test(size=(100, 40)) as pilot:
        app.query_one("#remove-account", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, Editor)
        assert get_account(engine, account.id) is not None
        await pilot.press("escape")
        assert get_account(engine, account.id) is not None

        app.query_one("#remove-account", Button).press()
        await pilot.pause()
        app.screen.query_one("#save", Button).press()
        await pilot.pause()
        assert get_account(engine, account.id) is None
        assert isinstance(app.screen, TaskLog)
        assert app.screen.finished
        app.screen.query_one("#close-task", Button).press()
        await pilot.pause()
        assert not isinstance(app.screen, (Editor, TaskLog))


async def test_failed_task_returns_to_form_with_inputs_preserved(engine):
    app = BotApp(engine)
    async with app.run_test(size=(100, 40)) as pilot:
        app.query_one("#new-project", Button).press()
        await pilot.pause()
        editor = app.screen
        editor.query_one("#name", Input).value = "Work"
        editor.query_one("#overlap_hours", Input).value = "0"
        editor.query_one("#save", Button).press()
        await pilot.pause()
        assert isinstance(app.screen, TaskLog)
        assert app.screen.finished
        assert not app.screen.succeeded
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is editor
        assert editor.query_one("#name", Input).value == "Work"
