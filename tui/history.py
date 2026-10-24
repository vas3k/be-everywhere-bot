from rich.text import Text
from textual.widgets import RichLog

from db.history import recent_history


class RecentHistory(RichLog):
    def __init__(self, engine):
        super().__init__(id="recent-history", wrap=True, markup=False, auto_scroll=False)
        self.engine = engine
        self.border_title = "Recent history · newest first · UTC"

    def on_mount(self):
        entries = recent_history(self.engine)
        if not entries:
            self.write(Text("No recorded history yet.", style="dim"))
            return
        colors = {
            "SYNCED": "green", "CYCLE": "cyan", "ERRORS": "red",
            "ACCOUNT": "blue", "REMOVED": "yellow", "PROJECT": "magenta",
        }
        for entry in entries:
            line = Text(entry.occurred_at.strftime("%Y-%m-%d %H:%M:%S"), style="dim")
            line.append(f"  {entry.kind:<7}", style=f"bold {colors[entry.kind]}")
            line.append(f"  {entry.project}", style="bold")
            line.append(f"\n  {entry.summary}")
            if entry.detail:
                line.append("\n  " + entry.detail.replace("\n", "\n  "), style="dim")
            self.write(line)
            self.write("")
