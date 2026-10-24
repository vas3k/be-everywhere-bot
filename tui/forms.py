from textual.app import ComposeResult
from textual.containers import Grid, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.reactive import reactive
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

from apis.registry import NETWORK_APIS
from config import (
    ACCOUNT_SYNC_ROLES, SOURCE_ONLY_NETWORKS, POST_MIN_AGE_MINUTES,
    WATCH_OVERLAP_HOURS, WATCH_INITIAL_LOOKBACK_HOURS, BACKFILL_POST_DELAY_SECONDS,
)
from db.accounts import get_all_credentials


AUTH_FIELDS = {
    "twitter": [("bearer_token", "Bearer token"), ("username", "Username (without @)")],
    "telegram": [("bot_token", "Bot token"), ("channel_id", "Channel ID or @channel")],
    "mastodon": [("instance_url", "Instance URL"), ("access_token", "Access token")],
    "threads": [("access_token", "Access token"), ("app_secret", "App secret (optional)"), ("username", "Username (optional)")],
    "bluesky": [("handle", "Handle"), ("app_password", "App password"), ("pds_url", "PDS URL (optional)")],
    "instagram": [("access_token", "Access token"), ("app_id", "Facebook App ID (Facebook-login tokens only)"), ("app_secret", "App secret (optional)"), ("username", "Username (optional)")],
    "linkedin": [("access_token", "Access token"), ("display_name", "Display name (optional)")],
    "rss": [("feed_url", "Feed URL")],
}
ROLE_LABELS = {
    "source_and_destination": "Source + destination",
    "source_only": "Source only",
    "destination_only": "Destination only",
}


class CredentialFields(Vertical):
    network = reactive("twitter", recompose=True)

    def __init__(self, network, credentials):
        super().__init__(id="credentials")
        self.network = network
        self.credentials = credentials

    def compose(self) -> ComposeResult:
        for key, label in AUTH_FIELDS[self.network]:
            secret = any(part in key for part in ("token", "password", "secret"))
            yield Label(label)
            yield Input(self.credentials.get(key, ""), id=f"credential-{key}", password=secret)
        help_text = getattr(NETWORK_APIS[self.network], "AUTH_HELP", "")
        if help_text:
            yield Static(help_text.strip(), markup=False, classes="muted")


class Editor(ModalScreen):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, kind, *, project=None, account=None, scope=None):
        super().__init__()
        self.kind = kind
        self.project = project
        self.account = account
        self.scope = scope

    def compose(self) -> ComposeResult:
        titles = {
            "project": "Project settings" if self.project else "Create project",
            "account": "Edit connection", "auth": "Authorize connection",
            "backfill": "Backfill posts", "remove": "Remove connection?",
        }
        with Vertical(id="dialog"):
            yield Label(titles[self.kind], classes="title")
            with VerticalScroll(id="fields"):
                if self.kind == "project":
                    yield Label("Name")
                    yield Input(self.project.name if self.project else "", id="name", disabled=bool(self.project and self.project.id == 1))
                    yield Checkbox("Enabled", value=self.project.enabled if self.project else True, id="enabled")
                    for key, label, default in (
                        ("min_age_minutes", "Minimum root age · minutes", POST_MIN_AGE_MINUTES),
                        ("overlap_hours", "Fetch overlap · hours", WATCH_OVERLAP_HOURS),
                        ("initial_lookback_hours", "Initial lookback · hours", WATCH_INITIAL_LOOKBACK_HOURS),
                        ("backfill_delay_seconds", "Backfill pause · seconds", BACKFILL_POST_DELAY_SECONDS),
                    ):
                        yield Label(label)
                        yield Input(str(getattr(self.project, key) if self.project else default), id=key)
                elif self.kind in ("account", "auth"):
                    network = self.account.network if self.account else "twitter"
                    yield Label("Network")
                    yield Select([(name.title(), name) for name in NETWORK_APIS], value=network, allow_blank=False, id="network", disabled=self.account is not None)
                    yield Label("Label · unique within this project and network")
                    yield Input(self.account.label if self.account else "", id="label", disabled=self.kind == "auth" and self.account is not None)
                    yield Label("Sync role")
                    role = self.account.sync_role if self.account else "source_and_destination"
                    yield Select([(ROLE_LABELS[item], item) for item in ACCOUNT_SYNC_ROLES], value=role, allow_blank=False, id="role", disabled=network in SOURCE_ONLY_NETWORKS)
                    yield Static("Shared remote accounts must be destination-only in every project.", classes="muted")
                    if self.kind == "auth":
                        credentials = get_all_credentials(self.app.engine, self.account.id) if self.account else {}
                        yield CredentialFields(network, credentials)
                elif self.kind == "backfill":
                    yield Static(f"Publish original posts since this UTC date to enabled destinations.\nScope: {self.scope}\nThe minimum-age gate is bypassed; existing deliveries are not repeated.", markup=False)
                    yield Label("Start date · YYYY-MM-DD")
                    yield Input(placeholder="2026-01-01", id="since")
                elif self.kind == "remove":
                    yield Static(
                        f"Remove {self.account.network} / {self.account.label}?\n\n"
                        "Credentials and pending work involving this connection will be removed. "
                        "Published posts stay on the network. Identity and mirror history are retained to prevent loops. "
                        "Re-adding creates a new connection with a new delivery history.", markup=False,
                    )
            yield Static("", id="form-error", markup=False)
            with Grid(classes="dialog-actions"):
                yield Button("Cancel", id="cancel")
                yield Button("Remove" if self.kind == "remove" else "Run backfill" if self.kind == "backfill" else "Save", id="save", variant="error" if self.kind == "remove" else "primary")

    def on_mount(self):
        if self.kind == "remove":
            self.query_one("#cancel", Button).focus()

    def on_select_changed(self, event: Select.Changed):
        if event.select.id != "network" or event.value is Select.BLANK:
            return
        role = self.query_one("#role", Select)
        role.disabled = event.value in SOURCE_ONLY_NETWORKS
        if role.disabled:
            role.value = "source_only"
        if self.kind == "auth":
            self.query_one(CredentialFields).network = event.value

    def action_cancel(self):
        if not self.app.busy:
            self.dismiss()

    async def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if self.app.busy:
            return
        if event.button.id == "cancel":
            self.dismiss()
        elif event.button.id == "save":
            await self.app.save_editor(self)
