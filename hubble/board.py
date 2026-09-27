"""Live dashboard for sub-agents: one row per agent with status, current action, tools, tokens, time."""

import threading
import time
from typing import Dict, List, Optional

from rich.console import Group
from rich.table import Table
from rich.text import Text

from hubble.spinner import GLYPHS, Shimmer

STATUS_ICON = {"done": ("✔", "bold green"), "failed": ("✘", "bold red"), "stopped": ("■", "yellow")}


class _Row:
    def __init__(self, label: str):
        self.label = label
        self.status = "running"   # running | done | failed | stopped
        self.action = "starting"
        self.tools = 0
        self.tokens = 0
        self.start = time.time()
        self.end: Optional[float] = None
        self.detail = ""

    @property
    def elapsed(self) -> float:
        return (self.end or time.time()) - self.start


def _fmt_tokens(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


class SubagentBoard:
    """Thread-safe model plus a rich renderable (Live calls __rich__ on every refresh)."""

    def __init__(self):
        self.rows: Dict[str, _Row] = {}
        self.order: List[str] = []
        self.lock = threading.Lock()
        self.start = time.time()
        self.header = Shimmer("Sub-agents exploring", details=False)

    # ----- updates (called from worker threads) --------------------------

    def add(self, key: str, label: str):
        with self.lock:
            if key not in self.rows:
                self.rows[key] = _Row(label)
                self.order.append(key)

    def step(self, key: str, action: str, is_tool: bool = True):
        with self.lock:
            row = self.rows.get(key)
            if row:
                row.action = action
                row.tools += int(is_tool)

    def tokens(self, key: str, n: int):
        with self.lock:
            row = self.rows.get(key)
            if row:
                row.tokens += n

    def finish(self, key: str, status: str, detail: str = ""):
        with self.lock:
            row = self.rows.get(key)
            if row and row.status == "running":
                row.status, row.detail, row.end = status, detail, time.time()

    # ----- rendering -------------------------------------------------------

    @property
    def counts(self):
        with self.lock:
            total = len(self.rows)
            done = sum(r.status != "running" for r in self.rows.values())
        return done, total

    def __rich__(self):
        with self.lock:
            rows = [self.rows[k] for k in self.order]
        done = sum(r.status != "running" for r in rows)
        running = len(rows) - done
        elapsed = int(time.time() - self.start)

        if running:
            self.header.set_label(f"{len(rows)} sub-agent{'s' if len(rows) != 1 else ''} exploring")
            head = self.header.__rich__()
            head.append(f"  {done}/{len(rows)} done · {elapsed}s · ctrl+c stops all", style="dim")
        else:
            failed = sum(r.status == "failed" for r in rows)
            head = Text.assemble(("● ", "bold magenta"),
                                 (f"{len(rows)} sub-agent{'s' if len(rows) != 1 else ''} finished", "bold"),
                                 (f"  {done - failed}/{len(rows)} succeeded · {elapsed}s", "dim"))

        label_w = min(max((len(r.label) for r in rows), default=8), 42)
        table = Table.grid(padding=(0, 1))
        table.add_column(no_wrap=True)                                     # indent + icon
        table.add_column(width=label_w, no_wrap=True, overflow="ellipsis")  # label
        table.add_column(justify="right", no_wrap=True, style="dim")       # stats
        table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")       # current action
        frame = int(time.time() * 10)
        for i, r in enumerate(rows):
            if r.status == "running":
                icon = Text("  " + GLYPHS[(frame + i * 3) % len(GLYPHS)], style="bold #ff875f")
                action = Text(r.action, style="#d7875f")
                label = Text(r.label, style="bold")
            else:
                ch, style = STATUS_ICON.get(r.status, ("•", "dim"))
                icon = Text("  " + ch, style=style)
                action = Text(r.detail or r.status, style="red" if r.status == "failed" else "dim")
                label = Text(r.label, style="bold" if r.status == "done" else "")
            stats = f"{r.tools:>2} tool{'s' if r.tools != 1 else ' '} · {_fmt_tokens(r.tokens):>5} tok · {int(r.elapsed):>3}s "
            table.add_row(icon, label, stats, action)
        return Group(head, table)
