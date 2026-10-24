from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Static


class TaskLog(ModalScreen[bool]):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, title):
        super().__init__()
        self.task_title = title
        self.finished = False
        self.succeeded = False

    def compose(self) -> ComposeResult:
        with Vertical(id="task-dialog"):
            yield Static(self.task_title, classes="title", markup=False)
            yield RichLog(id="task-log", wrap=True, markup=False, max_lines=10000)
            yield Static("Running… You can close this window when the task finishes.", id="task-status", markup=False)
            yield Button("Close", id="close-task", variant="primary", disabled=True)

    def action_close(self):
        if self.finished:
            self.dismiss(self.succeeded)

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "close-task":
            self.action_close()
