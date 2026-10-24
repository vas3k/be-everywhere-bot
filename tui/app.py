import logging
import re
from datetime import datetime, timezone
from math import isfinite

from rich.text import Text
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from textual.app import App, ComposeResult
from textual.reactive import reactive
from textual.containers import Grid, VerticalScroll
from textual.widgets import Button, Checkbox, DataTable, Footer, Header, Input, RichLog, Select, Static

from apis.registry import NETWORK_APIS
from db.accounts import (
    account_display_name, find_account, get_account, get_all_credentials, list_accounts,
    remove_account, set_account_role,
)
from db.projects import configure_project, create_project, get_project, list_projects
from db.schema import account_sync_state, mirrored_posts, source_posts, sync_batches, sync_mappings
from sync.engine import run_sync
from tui.forms import AUTH_FIELDS, Editor, ROLE_LABELS
from tui.task_log import TaskLog
from tui.history import RecentHistory


class UILogHandler(logging.Handler):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        message = self.format(record)
        for secret in self.app.secrets:
            if secret:
                message = message.replace(secret, "••••")
        message = re.sub(r"(/bot)[^/\s]+", r"\1••••", message)
        message = re.sub(r"((?:access_token|client_secret|appsecret_proof)=)[^&\s\"]+", r"\1••••", message)
        window = self.app.task_window
        if window is not None and window.is_mounted:
            window.query_one("#task-log", RichLog).write(Text(message))


class BotApp(App):
    TITLE = "Be Everywhere"
    SUB_TITLE = "Project control"
    CSS_PATH = "app.tcss"
    BINDINGS = [("escape", "home", "Projects"), ("r", "refresh_data", "Refresh"), ("ctrl+q", "quit", "Quit")]
    ENABLE_COMMAND_PALETTE = False
    selected_account_id = reactive(None, init=False)

    def __init__(self, engine, *, project_id=None):
        super().__init__()
        self.engine = engine
        self.project_id = project_id
        self.busy = False
        self.task_window = None
        self.secrets = set()
        self.statistics = {}
        self.connections = []
        self.theme = "tokyo-night"

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="body")
        yield Static("Ready · operations run one at a time · times are UTC", id="status", markup=False)
        yield Footer()

    async def on_mount(self):
        self.log_handler = UILogHandler(self)
        self.previous_handlers = logging.root.handlers[:]
        logging.root.handlers = [self.log_handler]
        await self.reload()

    def on_unmount(self):
        if hasattr(self, "previous_handlers"):
            logging.root.handlers = self.previous_handlers
            self.log_handler.close()

    def on_resize(self, event):
        for cards in self.query("#cards"):
            cards.styles.grid_size_columns = 2 if event.size.width >= 100 else 1

    async def action_home(self):
        if self.busy or isinstance(self.screen, (Editor, TaskLog)):
            return
        self.project_id = None
        self.selected_account_id = None
        await self.reload()

    async def action_refresh_data(self):
        if not self.busy and not isinstance(self.screen, (Editor, TaskLog)):
            await self.reload()

    def action_quit(self):
        if self.busy:
            self.notify("Wait for the current operation to finish.")
            return
        self.exit()

    async def reload(self):
        self.connections = list_accounts(self.engine)
        self.statistics = {account.id: {} for account in self.connections}
        with self.engine.connect() as connection:
            for name, table, column in (
                ("cached", source_posts, source_posts.c.account_id),
                ("pending_out", sync_batches, sync_batches.c.source_account_id),
                ("pending_in", sync_batches, sync_batches.c.dest_account_id),
                ("sent", sync_mappings, sync_mappings.c.source_account_id),
                ("received", sync_mappings, sync_mappings.c.dest_account_id),
                ("mirrors", mirrored_posts, mirrored_posts.c.account_id),
            ):
                for account_id, count in connection.execute(select(column, func.count()).group_by(column)):
                    if account_id in self.statistics:
                        self.statistics[account_id][name] = count
            for account_id, fetched in connection.execute(select(account_sync_state.c.account_id, account_sync_state.c.last_synced_at)):
                if account_id in self.statistics:
                    self.statistics[account_id]["fetched"] = fetched
        for account in self.connections:
            for key, value in get_all_credentials(self.engine, account.id).items():
                if any(part in key for part in ("token", "password", "secret", "jwt")) and not key.endswith("_at"):
                    self.secrets.add(value)

        body = self.query_one("#body", VerticalScroll)
        await body.remove_children()
        if self.project_id is None:
            await body.mount(Static("YOUR PROJECTS", classes="title"), Static("Select a project to manage its connections.", classes="muted"))
            cards = Grid(id="cards")
            cards.styles.grid_size_columns = 2 if self.size.width >= 100 else 1
            await body.mount(cards)
            for project in list_projects(self.engine):
                accounts = [account for account in self.connections if account.project_id == project.id]
                pending = sum(self.statistics[account.id].get("pending_out", 0) for account in accounts)
                last = project.last_synced_at.strftime("%d %b %H:%M") if project.last_synced_at else "never"
                label = Text(f"{project.name}\n", style="bold")
                label.append(f"{'● Enabled' if project.enabled else '○ Paused'} · {len(accounts)} connections\n", style="cyan" if project.enabled else "dim")
                label.append(f"Last success  {last} UTC\n{project.last_sync_count} delivered · {project.last_sync_error_count} errors · {pending} pending\nAge {project.min_age_minutes}m · overlap {project.overlap_hours}h", style="default")
                await cards.mount(Button(label, id=f"project-{project.id}", classes="project-card"))
            await body.mount(Static(f"Database: {self.engine.url.database}", markup=False, classes="muted"))
            await body.mount(Grid(
                Button("＋ New project", id="new-project", variant="primary"),
                Button("Sync all now", id="sync-all", variant="success"),
                Button("Backfill all", id="backfill-all"), Button("Refresh", id="refresh"),
                classes="actions",
            ))
            await body.mount(RecentHistory(self.engine))
        else:
            project = get_project(self.engine, self.project_id)
            await body.mount(Static(project.name, classes="title", markup=False))
            summary = (
                f"{'Enabled' if project.enabled else 'Paused'} · age {project.min_age_minutes}m · overlap {project.overlap_hours}h · "
                f"lookback {project.initial_lookback_hours}h · backfill pause {project.backfill_delay_seconds:g}s\n"
                f"Last success: {project.last_synced_at or 'never'}\n"
                f"Last cycle: {project.last_sync_started_at or 'never'} → {project.last_sync_finished_at or '—'}\n"
                f"{project.last_sync_count} mappings delivered · {project.last_sync_error_count} errors · created {project.created_at:%Y-%m-%d}"
            )
            await body.mount(Static(summary, markup=False, classes="summary"))
            await body.mount(Grid(
                Button("← Projects", id="home"), Button("Settings", id="settings"),
                Button("Sync project", id="sync-project", variant="success", disabled=not project.enabled),
                Button("Backfill project", id="backfill-project", disabled=not project.enabled), classes="actions",
            ))
            table = DataTable(id="accounts", cursor_type="row", zebra_stripes=True)
            table.add_column("Network", width=10)
            table.add_column("Account", width=28)
            table.add_column("Role", width=12)
            table.add_column("Last fetch · UTC", width=16)
            table.add_column("Label", width=16)
            table.add_column("Pending ↗/↙", width=12)
            accounts = [account for account in self.connections if account.project_id == self.project_id]
            for account in accounts:
                stats = self.statistics[account.id]
                role = {"source_and_destination": "Source + dest", "source_only": "Source", "destination_only": "Destination"}[account.sync_role]
                fetched = stats.get("fetched")
                last_fetch = fetched.strftime("%Y-%m-%d %H:%M") if fetched else "—"
                table.add_row(
                    Text(account.network),
                    Text(account_display_name(account, self.engine), overflow="ellipsis", no_wrap=True),
                    role, last_fetch, Text(account.label, overflow="ellipsis", no_wrap=True),
                    f"{stats.get('pending_out', 0)} / {stats.get('pending_in', 0)}",
                    key=str(account.id),
                )
            table.styles.height = min(9, max(3, len(accounts) + 1))
            await body.mount(table, Static("", id="account-detail", markup=False))
            await body.mount(Grid(
                Button("＋ Add account", id="add-account", variant="primary"),
                Button("Edit", id="edit-account"), Button("Reauthorize", id="reauth"),
                Button("Sync source", id="sync-account", variant="success"),
                Button("Backfill source", id="backfill-account"), Button("Remove", id="remove-account", variant="error"),
                classes="actions",
            ))
            ids = [account.id for account in accounts]
            if self.selected_account_id not in ids:
                self.selected_account_id = ids[0] if ids else None
            if self.selected_account_id is not None:
                table.move_cursor(row=ids.index(self.selected_account_id))
            self.watch_selected_account_id()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        if event.row_key.value is not None:
            self.selected_account_id = int(event.row_key.value)

    def watch_selected_account_id(self):
        if not self.screen_stack or not self.query("#account-detail"):
            return
        account = get_account(self.engine, self.selected_account_id) if self.selected_account_id else None
        if account:
            stats = self.statistics.get(account.id, {})
            credentials = get_all_credentials(self.engine, account.id)
            token_dates = " · ".join(f"{key}: {value}" for key, value in credentials.items() if key in ("token_expires_at", "token_renewed_at", "token_renew_attempted_at"))
            detail = (
                f"{account_display_name(account, self.engine)} · {ROLE_LABELS[account.sync_role]}\n"
                f"ID {account.id} · remote {account.remote_id} · last fetch {stats.get('fetched') or 'never'} UTC\n"
                f"{stats.get('cached', 0)} cached posts · {stats.get('sent', 0)} sent / {stats.get('received', 0)} received mappings · "
                f"{stats.get('mirrors', 0)} bot-created posts"
            )
            if token_dates:
                detail += "\n" + token_dates
        else:
            detail = "No connections yet. Add an account to start building this project's mesh."
        self.query_one("#account-detail", Static).update(detail)
        for selector in ("#edit-account", "#reauth", "#remove-account", "#sync-account", "#backfill-account"):
            buttons = self.query(selector)
            if buttons:
                buttons.first(Button).disabled = account is None or (selector in ("#sync-account", "#backfill-account") and (account.sync_role == "destination_only" or not get_project(self.engine, self.project_id).enabled))

    async def run_operation(self, title, operation):
        if self.busy or isinstance(self.screen, TaskLog):
            return False
        editor = self.screen if isinstance(self.screen, Editor) else None
        window = TaskLog(title)

        def task_closed(succeeded):
            self.task_window = None
            if succeeded and editor is not None:
                editor.dismiss()

        self.busy = True
        self.task_window = window
        await self.push_screen(window, task_closed)
        self.query_one("#status", Static).update(f"Running · {title}")
        success = False
        try:
            logging.info("%s", title)
            result = await operation()
            logging.info("%s — complete%s", title, f" ({result} mappings delivered)" if isinstance(result, int) else "")
            success = True
        except IntegrityError:
            message = "That name or label is already in use. Choose another."
            logging.error(message)
            if editor is not None:
                editor.query_one("#form-error", Static).update(message)
        except ValueError as exc:
            logging.error("%s: %s", title, exc)
            if editor is not None:
                editor.query_one("#form-error", Static).update("Please correct the values and try again.")
        except Exception:
            logging.exception("%s failed", title)
            if editor is not None:
                editor.query_one("#form-error", Static).update("Operation failed. Correct the values or retry.")
        finally:
            self.busy = False
            self.query_one("#status", Static).update("Ready · " + ("completed" if success else "last operation failed"))
            window.succeeded = success
            window.finished = True
            window.query_one("#task-status", Static).update("Completed" if success else "Failed · review the log before closing")
            window.query_one("#close-task", Button).disabled = False
            window.query_one("#close-task", Button).focus()
            await self.reload()
        return success

    async def save_editor(self, editor):
        async def save():
            if editor.kind == "project":
                name = editor.query_one("#name", Input).value.strip()
                values = {key: int(editor.query_one(f"#{key}", Input).value) for key in ("min_age_minutes", "overlap_hours", "initial_lookback_hours")}
                values["backfill_delay_seconds"] = float(editor.query_one("#backfill_delay_seconds", Input).value)
                values["enabled"] = editor.query_one("#enabled", Checkbox).value
                # Validate before creation so invalid settings cannot leave an empty project behind.
                if not name or values["min_age_minutes"] < 0 or values["overlap_hours"] <= 0 or values["initial_lookback_hours"] <= 0 or not isfinite(values["backfill_delay_seconds"]) or values["backfill_delay_seconds"] < 0:
                    raise ValueError("Enter a name, nonnegative age/delay and positive overlap/lookback.")
                project = editor.project or create_project(self.engine, name)
                configure_project(self.engine, project.id, name=name, **values)
                self.project_id = project.id
            elif editor.kind == "account":
                set_account_role(self.engine, editor.account.id, editor.query_one("#role", Select).value, label=editor.query_one("#label", Input).value)
            elif editor.kind == "auth":
                network = editor.query_one("#network", Select).value
                label = editor.query_one("#label", Input).value.strip()
                if not label:
                    raise ValueError("Enter a connection label")
                if editor.account is None and find_account(self.engine, network, label, project_id=self.project_id):
                    raise ValueError("This label already exists. Select that connection and choose Reauthorize.")
                credentials = {key: editor.query_one(f"#credential-{key}", Input).value.strip() for key, _ in AUTH_FIELDS[network]}
                self.secrets.update(value for key, value in credentials.items() if any(part in key for part in ("token", "secret", "password")))
                account = await NETWORK_APIS[network].authenticate(
                    self.engine, label=label, project_id=self.project_id,
                    sync_role=editor.query_one("#role", Select).value, credentials=credentials,
                )
                self.selected_account_id = account.id
            elif editor.kind == "remove":
                remove_account(self.engine, editor.account.id)
                self.selected_account_id = None
            elif editor.kind == "backfill":
                since = datetime.strptime(editor.query_one("#since", Input).value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                return await run_sync(self.engine, since=since, enforce_min_age=False,
                                      project_id=editor.project.id if editor.project else None,
                                      account_id=editor.account.id if editor.account else None)
        return await self.run_operation(editor.query_one(".title", Static).content, save)

    async def on_button_pressed(self, event: Button.Pressed):
        if self.busy:
            return
        action = event.button.id or ""
        if action.startswith("project-"):
            self.project_id = int(action.removeprefix("project-"))
            self.selected_account_id = None
            await self.reload()
        elif action == "home":
            await self.action_home()
        elif action == "refresh":
            await self.reload()
        elif action == "new-project":
            self.push_screen(Editor("project"))
        elif action == "settings":
            self.push_screen(Editor("project", project=get_project(self.engine, self.project_id)))
        elif action == "add-account":
            self.push_screen(Editor("auth"))
        elif action in ("edit-account", "reauth", "remove-account"):
            account = get_account(self.engine, self.selected_account_id)
            if account:
                kind = {"edit-account": "account", "reauth": "auth", "remove-account": "remove"}[action]
                self.push_screen(Editor(kind, account=account))
        elif action.startswith("backfill-"):
            project = get_project(self.engine, self.project_id) if action != "backfill-all" else None
            account = get_account(self.engine, self.selected_account_id) if action == "backfill-account" else None
            scope = f"{project.name} / {account.label}" if account else project.name if project else "all enabled projects"
            self.push_screen(Editor("backfill", project=project, account=account, scope=scope))
        elif action.startswith("sync-"):
            project_id = self.project_id if action != "sync-all" else None
            account_id = self.selected_account_id if action == "sync-account" else None

            async def sync():
                return await run_sync(self.engine, enforce_min_age=False, project_id=project_id, account_id=account_id)

            await self.run_operation("Force sync · " + ("all enabled projects" if project_id is None else get_project(self.engine, project_id).name), sync)
