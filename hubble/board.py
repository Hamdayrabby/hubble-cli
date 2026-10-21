"""Live dashboard for sub-agents: one row per agent with status, current action, tools, tokens, time."""

import threading
import time
from typing import Dict, List, Optional

from rich.console import Group
from rich.table import Table
from rich.text import Text

from hubble.spinner import GLYPHS, Shimmer

CLAUDE_GLYPHS = ["✻", "✼", "✽", "✾", "✿", "❀", "❁", "❂"]

STATUS_ICON = {"done": ("✔", "bold green"), "failed": ("✘", "bold red"), "stopped": ("■", "yellow"),
               "partial": ("◐", "bold yellow")}  # partial: ran out of steps, report may be incomplete


class _Row:
    def __init__(self, label: str, model: str = "", context_window: int = 0):
        self.label = label
        self.model = model
        self.context_window = context_window
        self.status = "running"   # running | done | failed | stopped
        self.action = "starting"
        self.tools = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self._tokens = 0
        self.context_tokens = 0
        self.start = time.time()
        self.end: Optional[float] = None
        self.detail = ""

    @property
    def tokens(self) -> int:
        return (self.prompt_tokens + self.completion_tokens) or self._tokens

    @tokens.setter
    def tokens(self, val: int):
        self._tokens = val

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

    def add(self, key: str, label: str, model: str = "", context_window: int = 0):
        with self.lock:
            if key not in self.rows:
                self.rows[key] = _Row(label, model=model, context_window=context_window)
                self.order.append(key)
            else:
                if model:
                    self.rows[key].model = model
                if context_window:
                    self.rows[key].context_window = context_window

    def set_model(self, key: str, model: str, context_window: int = 0):
        with self.lock:
            row = self.rows.get(key)
            if row:
                row.model = model
                if context_window:
                    row.context_window = context_window

    def step(self, key: str, action: str, is_tool: bool = True):
        with self.lock:
            row = self.rows.get(key)
            if row:
                row.action = action
                row.tools += int(is_tool)

    def tokens(self, key: str, n: int, prompt_tokens: int = 0, completion_tokens: int = 0, context_tokens: int = 0):
        with self.lock:
            row = self.rows.get(key)
            if row:
                if prompt_tokens or completion_tokens:
                    row.prompt_tokens += prompt_tokens
                    row.completion_tokens += completion_tokens
                else:
                    row._tokens += n
                if context_tokens:
                    row.context_tokens = context_tokens

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

    def to_formatted_text(self):
        """Build formatted text tuples for prompt_toolkit (Claude-style live preloader)."""
        import shutil
        with self.lock:
            rows = [self.rows[k] for k in self.order]
        if not rows:
            return []
        if all(r.status != "running" for r in rows):
            return []

        cols = shutil.get_terminal_size((80, 24)).columns
        frame = int(time.time() * 10)
        running = sum(r.status == "running" for r in rows)
        total = len(rows)
        done = total - running
        frags = []

        if len(rows) == 1:
            r = rows[0]
            glyph = CLAUDE_GLYPHS[frame % len(CLAUDE_GLYPHS)]
            frags.append(("fg:#00d7ff bold", f"{glyph} "))
            tag = f"{running} subagent running"
            frags.append(("fg:#c084fc bold", tag))
            frags.append(("fg:#6c6c80", " · "))
            frags.append(("bold", f"{r.label}"))
            stats = f" · {r.tools} tool{'s' if r.tools != 1 else ''} · {_fmt_tokens(r.tokens)} tok · {int(r.elapsed)}s · "
            frags.append(("fg:#6c6c80", stats))
            act = r.action or "running..."
            used_len = 2 + len(tag) + 3 + len(r.label) + len(stats)
            avail = max(35, cols - used_len - 3)
            if len(act) > avail:
                act = act[:avail - 1] + "…"
            frags.append(("fg:#00d7ff", act))
            return frags

        # Multi-subagent orchestration
        header_glyph = CLAUDE_GLYPHS[frame % len(CLAUDE_GLYPHS)]
        elapsed = int(time.time() - self.start)
        header_tag = f"Orchestrating {total} subagents"
        header_stats = f" ({running} running · {done} done · {elapsed}s)"
        frags.append(("fg:#00d7ff bold", f"{header_glyph} "))
        frags.append(("fg:#c084fc bold", header_tag))
        frags.append(("fg:#6c6c80", header_stats))

        for i, r in enumerate(rows):
            frags.append(("", "\n"))
            if r.status == "running":
                glyph = CLAUDE_GLYPHS[(frame + i * 3) % len(CLAUDE_GLYPHS)]
                frags.append(("fg:#00d7ff bold", f"  {glyph} "))
                frags.append(("bold", f"{r.label}"))
                stats = f" · {r.tools} tool{'s' if r.tools != 1 else ''} · {_fmt_tokens(r.tokens)} tok · {int(r.elapsed)}s · "
                frags.append(("fg:#6c6c80", stats))
                act = r.action or "running..."
                used_len = 4 + len(r.label) + len(stats)
                avail = max(25, cols - used_len - 3)
                if len(act) > avail:
                    act = act[:avail - 1] + "…"
                frags.append(("fg:#00d7ff", act))
            else:
                ch, _ = STATUS_ICON.get(r.status, ("•", "dim"))
                style_pt = "fg:#50fa7b bold" if r.status == "done" else "fg:#ff5555 bold" if r.status == "failed" else "fg:#f1fa8c bold"
                frags.append((style_pt, f"  {ch} "))
                frags.append(("bold" if r.status == "done" else "", f"{r.label}"))
                stats = f" · {r.tools} tool{'s' if r.tools != 1 else ''} · {_fmt_tokens(r.tokens)} tok · {int(r.elapsed)}s · "
                frags.append(("fg:#6c6c80", stats))
                frags.append(("fg:#ff5555" if r.status == "failed" else "fg:#6c6c80", r.detail or r.status))
        return frags

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
            ok = sum(r.status == "done" for r in rows)
            partial = sum(r.status == "partial" for r in rows)
            head = Text.assemble(("● ", "bold magenta"),
                                 (f"{len(rows)} sub-agent{'s' if len(rows) != 1 else ''} finished", "bold"),
                                 (f"  {ok}/{len(rows)} succeeded" + (f", {partial} partial" if partial else "")
                                  + f" · {elapsed}s", "dim"))

        label_w = min(max((len(r.label) for r in rows), default=8), 42)
        table = Table.grid(padding=(0, 1))
        table.add_column(no_wrap=True)                                     # indent + icon
        table.add_column(width=label_w, no_wrap=True, overflow="ellipsis")  # label
        table.add_column(justify="right", no_wrap=True, style="dim")       # stats
        table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")       # current action
        frame = int(time.time() * 10)
        for i, r in enumerate(rows):
            if r.status == "running":
                icon = Text("  " + CLAUDE_GLYPHS[(frame + i * 3) % len(CLAUDE_GLYPHS)], style="bold #00d7ff")
                action = Text(r.action, style="#00d7ff" if any(w in r.action.lower() for w in ("searching", "reading", "fetching", "thinking", "symbols", "running")) else "#d7875f")
                label = Text(r.label, style="bold")
            else:
                ch, style = STATUS_ICON.get(r.status, ("•", "dim"))
                icon = Text("  " + ch, style=style)
                action = Text(r.detail or r.status, style="red" if r.status == "failed" else "dim")
                label = Text(r.label, style="bold" if r.status == "done" else "")
            stats = f"{r.tools:>2} tool{'s' if r.tools != 1 else ' '} · {_fmt_tokens(r.tokens):>5} tok · {int(r.elapsed):>3}s "
            table.add_row(icon, label, stats, action)
        if len(rows) > 1:
            return Group(head, table)
        return table
