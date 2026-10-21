"""Terminal rendering: streamed markdown, tool call lines, diffs and approval prompts."""

import json
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from hubble.agent import Events, describe_call
from hubble.board import SubagentBoard
from hubble.provider import TurnResult, strip_thinking
from hubble.spinner import is_prompt_proxy_active, start_shimmer
from hubble.tools import Tool, ToolContext

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

from hubble.theme import get_palette, get_rich_theme

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

ACTIVE_THEME = "terminal"
ASSISTANT_ICON = "🔭"
ASSISTANT_TITLE = "🔭 Hubble"


def apply_theme(name: str):
    """Update active theme across consoles and diff renderers."""
    global ACTIVE_THEME
    ACTIVE_THEME = name or "terminal"
    theme = get_rich_theme(ACTIVE_THEME)
    console._theme = theme
    err_console._theme = theme


TOOL_LABELS = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit", "apply_patch": "Patch",
               "shell": "Shell", "manage_task": "Task Supervisor", "grep": "Search", "glob": "Glob",
               "list_dir": "List", "symbols": "Symbols", "todo_write": "Todos", "task": "Task",
               "invoke_subagent": "Subagent", "define_subagent": "Define Subagent",
               "web_search": "Web Search", "web_fetch": "Fetch"}
MAX_DIFF_LINES = 120


def render_diff(diff: str, max_lines: int = MAX_DIFF_LINES) -> Text:
    out = Text()
    lines = diff.splitlines()
    pal = get_palette(ACTIVE_THEME)

    hunk_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(.*)$")

    add_style = f"{pal.diff_add_bg} {pal.diff_add_fg}".strip() if pal.diff_add_bg else "green"
    del_style = f"{pal.diff_del_bg} {pal.diff_del_fg}".strip() if pal.diff_del_bg else "red"
    gutter_style = pal.diff_num or "dim"
    sep_style = "dim"
    accent_style = pal.accent or "cyan"

    old_ln = 0
    new_ln = 0
    in_hunk = False
    displayed = 0

    for line in lines:
        if displayed >= max_lines:
            break

        m = hunk_re.match(line)
        if m:
            in_hunk = True
            old_ln = int(m.group(1))
            new_ln = int(m.group(3))
            heading = m.group(5).strip()
            out.append(f"{'':>4} │ {'':>4} │ ", style=gutter_style)
            out.append(f"@@ -{m.group(1)} +{m.group(3)} @@", style=accent_style)
            if heading:
                out.append(f" {heading}", style="dim")
            out.append("\n")
            displayed += 1
            continue

        if line.startswith(("---", "+++")):
            out.append(line + "\n", style="bold")
            displayed += 1
            continue

        if line.startswith("\\"):
            out.append(f"{'':>4} │ {'':>4} │ {line}\n", style="dim")
            displayed += 1
            continue

        if in_hunk:
            if line.startswith("+"):
                code = line[1:]
                out.append(f"{'':>4} ", style=gutter_style)
                out.append("│", style=sep_style)
                out.append(f" {new_ln:>4} ", style=gutter_style)
                out.append("│", style=sep_style)
                out.append(f" +{code}\n", style=add_style)
                new_ln += 1
                displayed += 1
            elif line.startswith("-"):
                code = line[1:]
                out.append(f"{old_ln:>4} ", style=gutter_style)
                out.append("│", style=sep_style)
                out.append(f"{'':>4} ", style=gutter_style)
                out.append("│", style=sep_style)
                out.append(f" -{code}\n", style=del_style)
                old_ln += 1
                displayed += 1
            else:
                code = line[1:] if line.startswith(" ") else line
                out.append(f"{old_ln:>4} ", style=gutter_style)
                out.append("│", style=sep_style)
                out.append(f" {new_ln:>4} ", style=gutter_style)
                out.append("│", style=sep_style)
                out.append(f"  {code}\n", style="dim")
                old_ln += 1
                new_ln += 1
                displayed += 1
        else:
            if line.startswith("+"):
                out.append(line + "\n", style=add_style)
            elif line.startswith("-"):
                out.append(line + "\n", style=del_style)
            else:
                out.append(line + "\n", style="dim")
            displayed += 1

    if len(lines) > max_lines:
        out.append(f"... {len(lines) - max_lines} more diff lines\n", style="dim")
    return out


class _BlockPreview:
    """The block still being written, redrawn smoothly with a cursor."""

    def __init__(self, owner: "StreamingMarkdown"):
        self.owner = owner

    def __rich__(self):
        text, typing = self.owner.visible()
        if not text.strip():
            return Text("▌", style="#8787af") if typing else Text("")
        lines = text.splitlines()
        room = max(4, self.owner.console.height - 4)
        if len(lines) > room:
            lines = lines[-room:]
        preview_text = "\n".join(lines)
        out = Text(preview_text)
        if typing:
            out.append(" ▌", style="bold #8787af")
        return out


class StreamingMarkdown:
    """Renders markdown as it streams, like a chat app typing it out.

    Everything received goes into `buf`; the screen reveals it word by word at a steady pace,
    speeding up whenever a big chunk arrives, so it stays within about half a second of the model.
    Finished blocks are printed for good, formatted; the block still being typed shows live
    underneath them.
    """

    MIN_CPS = 280         # smooth, responsive typing (~55 words a second)
    CATCH_UP_S = 0.12     # speed = backlog / this: accelerates quickly to match fast models
    FINISH_S = 0.10       # at the end of a reply, finish immediately without pausing

    def __init__(self, con: Console):
        import threading
        self.console = con
        self.buf = ""
        self.shown = 0.0      # characters of buf revealed so far
        self.printed_any = False
        self.live = None
        self._last = 0.0
        self._lock = threading.Lock()
        self.animate = con.is_terminal and not is_prompt_proxy_active()

    # ----- reveal -------------------------------------------------------------

    def _advance(self):
        now = time.time()
        if not self.animate:
            self.shown = len(self.buf)
        else:
            dt = min(now - self._last, 0.2) if self._last else 0.0
            backlog = len(self.buf) - self.shown
            if backlog > 0:
                self.shown = min(len(self.buf), self.shown + max(self.MIN_CPS, backlog / self.CATCH_UP_S) * dt)
        self._last = now

    def _visible_end(self) -> int:
        """End of the revealed text, snapped back to a word boundary (so whole words appear)."""
        end = int(self.shown)
        if end >= len(self.buf):
            return len(self.buf)
        space = max(self.buf.rfind(" ", 0, end + 1), self.buf.rfind("\n", 0, end + 1))
        return space + 1 if space >= 0 and end - space < 40 else end

    def visible(self):
        with self._lock:
            self._advance()
            end = self._visible_end()
            return self.buf[:end], end < len(self.buf)

    # ----- feeding --------------------------------------------------------------

    def feed(self, delta: str):
        with self._lock:
            self.buf += delta
            self._advance()
            cut = self._boundary(self.buf[:self._visible_end()])
            if cut:
                chunk, self.buf = self.buf[:cut], self.buf[cut:]
                self.shown = max(0.0, self.shown - cut)
        if cut:
            self._stop_live()
            self._render(chunk)
        if self.animate and self.live is None and self.buf.strip() and self.console.is_terminal and not is_prompt_proxy_active():
            from rich.live import Live
            self._last = time.time()
            self.live = Live(_BlockPreview(self), console=self.console, refresh_per_second=30,
                             transient=True, vertical_overflow="crop")
            self.live.start()

    def _stop_live(self):
        if self.live is not None:
            try:
                self.live.stop()
            finally:
                self.live = None

    def flush(self):
        # Let the typing finish (briefly) instead of the rest of the reply appearing at once.
        if self.live is not None:
            deadline = time.time() + self.FINISH_S
            while time.time() < deadline:
                with self._lock:
                    self._advance()
                    done = self.shown >= len(self.buf)
                if done:
                    break
                time.sleep(1 / 30)
        self._stop_live()
        if self.buf.strip():
            self._render(self.buf)
        self.buf = ""
        self.shown = 0.0

    @staticmethod
    def _boundary(text: str) -> Optional[int]:
        in_fence, pos, last = False, 0, None
        for line in text.splitlines(keepends=True):
            if not line.endswith("\n"):
                break
            pos += len(line)
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                in_fence = not in_fence
                if not in_fence:
                    last = pos
            elif not stripped and not in_fence:
                last = pos
        return last

    def _render(self, chunk: str):
        if not chunk.strip():
            return
        pal = get_palette(ACTIVE_THEME)
        code_theme = getattr(pal, "code_theme", "monokai") or "monokai"
        self.console.print(Markdown(chunk.strip("\n"), code_theme=code_theme))
        self.printed_any = True


def activity_label(tool: Tool, args: Dict[str, Any]) -> str:
    """Words shown in the shimmer while a tool runs."""
    target = describe_call(tool, args)
    short = target if len(target) <= 48 else target[:45] + "..."
    return {
        "read_file": f"Reading {short}", "write_file": f"Writing {short}", "edit_file": f"Editing {short}",
        "apply_patch": f"Patching {short}",
        "shell": f"Running {short}", "grep": f"Searching for {short}", "glob": f"Finding {short}",
        "list_dir": f"Looking around {short}", "symbols": f"Searching symbols for {short}",
        "todo_write": "Updating the plan", "task": f"Researching {short}",
        "web_search": f"Searching the web for {short}", "web_fetch": f"Fetching {short}",
    }.get(tool.name, f"Running {tool.name}")


def summarize_output(tool: Tool, output: str, is_error: bool) -> Tuple[str, List[str]]:
    """(headline, extra lines) shown under a tool call."""
    lines = output.splitlines()
    if is_error:
        return lines[0][:200] if lines else "error", lines[1:4]
    if tool.name == "read_file":
        body = [l for l in lines[1:] if " | " in l]
        return f"Read {len(body)} lines", []
    if tool.name in ("grep", "glob"):
        if output.startswith("No "):
            return output.splitlines()[0], []
        return f"{len(lines)} result{'s' if len(lines) != 1 else ''}", lines[:3]
    if tool.name == "symbols":
        return lines[0][:200] if lines else "symbols found", lines[1:4]
    if tool.name == "apply_patch":
        return lines[0][:200] if lines else "patch applied", []
    if tool.name == "shell":
        exit_line = lines[-1] if lines else ""
        body = lines[:-1]
        return exit_line.strip("[]"), body[-8:]
    if tool.name == "web_search":
        hits = [l for l in lines if l[:3].strip().rstrip(".").isdigit()]
        return (f"{len(hits)} result{'s' if len(hits) != 1 else ''}" if hits else lines[0][:120]), \
            [h.split(". ", 1)[-1] for h in hits[:3]]
    if tool.name == "web_fetch":
        return f"Fetched {len(output):,} chars", lines[2:3]
    if tool.name in ("task", "invoke_subagent"):
        cleaned = strip_thinking(output).strip()
        lines = cleaned.splitlines()
        return (f"Report ready ({len(cleaned):,} chars)", lines[:2]) if lines else ("Report ready", [])
    return lines[0][:200] if lines else "done", []


class ThinkingAccordion:
    """Manages the live streaming and collapsed accordion state of model reasoning.

    While streaming:
      If show_live is True, dynamically updates a single-line preview with elapsed
      time and estimated token count:
      ▶ Thinking (1.8s · 340 tok)

    When finished (or upon receiving first answer token / tool call):
      Collapses cleanly into a single dim line:
      ▼ Thought for 3.2s (840 tokens)  · /thinking view to inspect
    """

    def __init__(self, con: Console, show_live: bool = False, theme_name: str = "terminal"):
        self.console = con
        self.show_live = show_live
        self.theme_name = theme_name
        self.chunks: List[str] = []
        self.t0 = time.time()
        self.t1: Optional[float] = None
        self.collapsed = False
        self.live = None
        self._last_snippet = ""
        self.char_count = 0

    def feed(self, delta: str):
        if not self.chunks:
            self.t0 = time.time()
        self.chunks.append(delta)
        self.char_count += len(delta)
        if self.show_live and self.console.is_terminal and not self.collapsed:
            self._update_live()

    def _update_live(self):
        if is_prompt_proxy_active():
            return
        tokens = int(self.char_count / 3.8)
        elapsed = time.time() - self.t0
        pal = get_palette(self.theme_name)
        icon_col = pal.thought_title or "cyan bold"
        text = Text()
        text.append("▶ Thinking ", style=icon_col)
        text.append(f"({elapsed:.1f}s · {tokens:,} tok)", style="dim")
        if self.live is None:
            from rich.live import Live
            self.live = Live(text, console=self.console, transient=True, refresh_per_second=10)
            self.live.start()
        else:
            self.live.update(text)

    def collapse(self):
        if self.collapsed:
            return
        self.collapsed = True
        self.t1 = time.time()
        if self.live is not None:
            try:
                self.live.stop()
            except Exception:
                pass
            self.live = None
        if not self.chunks:
            return
        elapsed = (self.t1 or time.time()) - self.t0
        tokens = int(self.char_count / 3.8)
        pal = get_palette(self.theme_name)
        icon_col = pal.thought_title or "cyan"
        summary = Text()
        summary.append("▼ Thought for ", style="dim")
        summary.append(f"{elapsed:.1f}s", style="dim bold")
        summary.append(f" ({tokens:,} tokens)", style="dim")
        summary.append("  · /thinking view", style="dim italic")
        self.console.print(summary)

    def get_full_text(self) -> str:
        return "".join(self.chunks)


class ReplEvents(Events):
    def __init__(self, ctx: ToolContext, show_reasoning: bool = False):
        self.ctx = ctx
        self.show_reasoning = show_reasoning
        self.reasoning_chars = 0
        self.answer_started = False
        self.tool_calls_this_turn = 0
        self.tool_summary_shown = False
        self.md: Optional[StreamingMarkdown] = None
        self.status = None
        self.in_reasoning = False
        self.approved_diff_shown = False
        self.batch = 0  # >0 while parallel sub-agents run: the board owns the screen
        self.board: Optional[SubagentBoard] = None
        self.board_live = None
        self.accordion: Optional[ThinkingAccordion] = None
        self.last_thinking = ""
        self.turn_start_time = 0.0
        self.current_activity: Optional[str] = None
        self.current_tool: Optional[str] = None
        self.approval_bridge = None

    # ----- sub-agent dashboard ---------------------------------------------

    def batch_start(self, count: int):
        self._stop_status(force=True)
        self.batch = count

    def batch_end(self):
        self.batch = 0
        self._close_board()

    def subagent_start(self, key: str, label: str, model: str = "", context_window: int = 0):
        if self.board is None:
            self._stop_status(force=True)
            self.board = SubagentBoard()
            if not is_prompt_proxy_active():
                try:
                    from rich.live import Live
                    self.board_live = Live(self.board, console=console, refresh_per_second=10, transient=True)
                    self.board_live.start()
                except Exception:
                    self.board_live = None
            else:
                self.board_live = None
        self.board.add(key, label, model=model, context_window=context_window)

    def subagent_step(self, key: str, action: str, is_tool: bool = True):
        if self.board:
            self.board.step(key, action, is_tool)

    def subagent_tokens(self, key: str, tokens: int, prompt_tokens: int = 0, completion_tokens: int = 0, context_tokens: int = 0):
        if self.board:
            self.board.tokens(key, tokens, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, context_tokens=context_tokens)

    def subagent_end(self, key: str, status: str, detail: str = ""):
        if self.board:
            self.board.finish(key, status, detail)

    def _close_board(self):
        """Stop the live board and leave its final state on screen."""
        if self.board_live is not None:
            try:
                self.board_live.stop()
            except Exception:
                pass
            self.board_live = None
        if self.board is not None:
            console.print(self.board)
            self.board = None

    def _stop_status(self, force: bool = False):
        if (self.batch or self.board_live is not None) and not force:
            return
        if self.status is not None:
            try:
                self.status.stop()
            except Exception:
                pass
            self.status = None

    def _end_reasoning(self):
        if self.in_reasoning:
            console.print()
            self.in_reasoning = False

    def _show_tool_summary_if_needed(self):
        if not self.tool_summary_shown and self.tool_calls_this_turn > 0:
            count = self.tool_calls_this_turn
            word = "tool call" if count == 1 else "tool calls"
            console.print(f"[dim]● {count} {word}[/dim]")
            self.tool_summary_shown = True

    def turn_start(self):
        self._stop_status(force=True)
        self.turn_start_time = time.time()
        self.current_activity = "Thinking..."
        self.md = StreamingMarkdown(console)
        self.reasoning_chars = 0
        self.answer_started = False
        self.tool_calls_this_turn = 0
        self.tool_summary_shown = False
        self.status = start_shimmer(console)
        self.accordion = ThinkingAccordion(console, show_live=self.show_reasoning, theme_name=ACTIVE_THEME)

    def reasoning(self, delta: str):
        if self.show_reasoning:
            self._stop_status(force=True)
        self.current_activity = "Thinking..."
        self.reasoning_chars += len(delta)
        if self.accordion:
            self.accordion.feed(delta)
        if self.status is not None and hasattr(self.status, "shimmer"):
            self.status.shimmer.tokens = int(self.reasoning_chars / 3.8)

    def text(self, delta: str):
        self._stop_status(force=True)
        self.current_activity = "Generating response..."
        if self.accordion and not self.accordion.collapsed:
            self.accordion.collapse()
            self.last_thinking = self.accordion.get_full_text()
        self._end_reasoning()
        if not self.answer_started:
            self._show_tool_summary_if_needed()
            self.answer_started = True
        if self.md:
            self.md.feed(delta)

    def turn_end(self, result: TurnResult):
        self._stop_status(force=True)
        self.current_activity = None
        self.current_tool = None
        if self.board is not None:
            self._close_board()
        if self.accordion and not self.accordion.collapsed:
            self.accordion.collapse()
            self.last_thinking = self.accordion.get_full_text()
        self._end_reasoning()
        self._show_tool_summary_if_needed()
        if self.md:
            self.md.flush()
            if self.md.printed_any:
                console.print()

    def tool_start(self, tool: Tool, args: Dict[str, Any]):
        if self.accordion and not self.accordion.collapsed:
            self.accordion.collapse()
            self.last_thinking = self.accordion.get_full_text()
        label = TOOL_LABELS.get(tool.name, tool.name)
        if tool.name in ("task", "invoke_subagent"):
            self._stop_status(force=True)
            return  # shown on the sub-agent board instead
        self.tool_calls_this_turn += 1
        self.current_tool = activity_label(tool, args)
        self.current_activity = self.current_tool
        self.approved_diff_shown = False
        if not self.batch:
            self._stop_status(force=True)
            self.status = start_shimmer(console, self.current_tool)

    def tool_result(self, tool: Tool, args: Dict[str, Any], output: str, is_error: bool):
        self.current_tool = None
        self.current_activity = "Processing results..."
        if tool.name in ("task", "invoke_subagent") and args:
            if not self.batch:
                self._close_board()  # single sub-agent: its board ends with it
            self._stop_status(force=True)
            return
        self._stop_status(force=True)
        if is_error:
            headline, extra = summarize_output(tool, output, is_error)
            if tool.name in ("task", "invoke_subagent") and args.get("description"):
                headline = f"{args['description']}: {headline}"
            style = "red"
            console.print(f"  [red]✘[/red] [{style}]{escape(headline)}[/{style}]")

    def notice(self, message: str, level: str = "info"):
        self._stop_status()
        style = {"warn": "yellow", "error": "bold red", "dim": "dim"}.get(level, "cyan")
        console.print(f"[{style}]{escape(message)}[/{style}]")

    def todos(self, todos: List[Dict[str, str]]):
        render_todos(todos)

    def ask(self, tool: Tool, args: Dict[str, Any], preview: Optional[str]) -> Tuple[str, str]:
        self._stop_status()
        # An edit sub-agent asks while its board is live: pause the board so it doesn't redraw
        # over the question, and bring it back afterwards.
        paused = self.board_live is not None
        if paused:
            self.board_live.stop()
        try:
            bridge = getattr(self, "approval_bridge", None)
            if bridge is not None:
                return bridge(tool, args, preview)
            return self._ask(tool, args, preview)
        finally:
            if paused and self.board_live is not None:
                self.board_live.start()

    def _ask(self, tool: Tool, args: Dict[str, Any], preview: Optional[str]) -> Tuple[str, str]:
        label = TOOL_LABELS.get(tool.name, tool.name)
        call_desc = describe_call(tool, args)
        title = f"{label} {call_desc}".strip()
        if preview and preview.startswith("---"):
            from rich import box
            console.print(Panel(render_diff(preview), title=f"[bold] {escape(title[:100])} [/bold]",
                                title_align="left", border_style="dim", box=box.ROUNDED, padding=(0, 1)))
        elif tool.kind == "exec":
            cmd = preview or args.get("command", "")
            console.print(f"  [cyan]$[/cyan] [bold white]{escape(cmd)}[/bold white]")
            if args.get("description"):
                console.print(f"    [dim]{escape(args['description'])}[/dim]")
        else:
            if call_desc:
                console.print(f"  [cyan]●[/cyan] [bold]{label}[/bold] [dim]{escape(call_desc)}[/dim]")
            else:
                console.print(f"  [cyan]●[/cyan] [bold]{label}[/bold]")
        always = {"edit": "edits", "web": tool.target(args), "mcp": tool.target(args)}.get(
            tool.kind, _command_family(args.get("command", "")))
        question = {"exec": "Run this command?", "edit": "Make this change?", "web": f"Fetch {always}?" if always else "Fetch this page?"}.get(
            tool.kind, f"Allow {label}?")
        can_stage_hunks = False
        patch_hunks: List[str] = []
        if tool.name == "apply_patch" and args.get("patch"):
            try:
                from hubble.tools import split_patch_into_hunks
                patch_hunks = split_patch_into_hunks(args["patch"])
                if len(patch_hunks) > 1:
                    can_stage_hunks = True
            except Exception:
                pass

        if sys.stdin.isatty() and console.is_terminal:
            options = ["Yes", f"Yes, and don't ask again for {always} this session"]
            if can_stage_hunks:
                options.append(f"Select individual hunks to stage ({len(patch_hunks)} hunks available)")
            options.append("No, and tell the model what to do instead")
            esc_idx = len(options) - 1

            choice = choose(question, options, esc_index=esc_idx)
            if choice is None:
                raise KeyboardInterrupt  # Ctrl+C: stop the whole turn
            if choice == 0:
                self.approved_diff_shown = True
                console.print("  [green]✔[/green] [dim]allowed[/dim]")
                return "yes", ""
            if choice == 1:
                self.approved_diff_shown = True
                console.print(f"  [green]✔[/green] [dim]allowed; won't ask again for {escape(always)} this session[/dim]")
                return "always", ""
            if can_stage_hunks and choice == 2:
                selected_indices = []
                console.print(f"\n[bold cyan]Interactive Hunk Staging for {escape(args.get('path', 'file'))} ({len(patch_hunks)} hunks):[/bold cyan]")
                for idx, hunk_str in enumerate(patch_hunks):
                    console.print(Panel(render_diff(hunk_str), title=f"[bold]Hunk {idx + 1} of {len(patch_hunks)}[/bold]", border_style="cyan"))
                    hunk_choice = choose(f"Stage hunk {idx + 1}?", ["Stage this hunk", "Skip this hunk", "Stage all remaining hunks", "Discard all remaining hunks"], esc_index=3)
                    if hunk_choice == 0:
                        selected_indices.append(idx)
                        console.print(f"  [green]✔[/green] Hunk {idx + 1} staged")
                    elif hunk_choice == 1:
                        console.print(f"  [yellow]○[/yellow] Hunk {idx + 1} skipped")
                    elif hunk_choice == 2:
                        selected_indices.extend(range(idx, len(patch_hunks)))
                        console.print(f"  [green]✔[/green] Staged all remaining hunks ({idx + 1} to {len(patch_hunks)})")
                        break
                    else:
                        console.print(f"  [red]✘[/red] Discarded remaining hunks")
                        break
                if selected_indices:
                    args["selected_hunks"] = selected_indices
                    console.print(f"  [green]✔[/green] Staging {len(selected_indices)} of {len(patch_hunks)} hunks")
                    self.approved_diff_shown = True
                    return "yes", ""
                else:
                    console.print("  [yellow]○[/yellow] No hunks staged.")
                    return "no", "User skipped all hunks during interactive hunk staging."
            feedback = ask_line("  Tell the model what to do instead (Enter to just say no): ")
            console.print("  [red]✘[/red] [dim]denied" + (f": {escape(feedback)}" if feedback else "") + "[/dim]")
            return "no", feedback
        # Not an interactive terminal (piped input): the plain typed answer.
        hunk_hint = "   [bold]s[/bold] stage hunks" if can_stage_hunks else ""
        console.print(f"  [bold]y[/bold] yes   [bold]a[/bold] always ({escape(always)} this session){hunk_hint}   "
                      f"[bold]n[/bold] no, tell the model why   [dim]Ctrl+C stop[/dim]")
        while True:
            try:
                answer = console.input("[bold yellow]  allow? [/bold yellow]").strip().lower()
            except EOFError:
                raise KeyboardInterrupt
            if answer in ("", "y", "yes"):
                self.approved_diff_shown = True
                return "yes", ""
            if answer in ("a", "always"):
                self.approved_diff_shown = True
                return "always", ""
            if can_stage_hunks and answer in ("s", "stage"):
                try:
                    raw_hunks = console.input(f"[dim]  comma-separated 1-based hunk indices to stage (1-{len(patch_hunks)}): [/dim]").strip()
                    picked = [int(p.strip()) - 1 for p in raw_hunks.split(",") if p.strip().isdigit()]
                    picked = [i for i in picked if 0 <= i < len(patch_hunks)]
                    if picked:
                        args["selected_hunks"] = picked
                        self.approved_diff_shown = True
                        return "yes", ""
                except Exception:
                    pass
                return "no", "Hunk staging cancelled."
            if answer in ("n", "no"):
                try:
                    feedback = console.input("[dim]  feedback for the model (optional): [/dim]").strip()
                except EOFError:
                    feedback = ""
                return "no", feedback
            if answer.startswith("n "):
                return "no", answer[2:].strip()
            console.print(f"[dim]  type y, a{', s' if can_stage_hunks else ''} or n[/dim]")


def render_todos(todos: List[Dict[str, str]]):
    if not todos:
        return
    marks = {"completed": "[green]✔[/green]", "in_progress": "[yellow]◐[/yellow]", "pending": "[dim]○[/dim]"}
    for t in todos:
        text = escape(t["content"])
        if t["status"] == "completed":
            text = f"[dim strike]{text}[/dim strike]"
        elif t["status"] == "in_progress":
            text = f"[bold]{text}[/bold]"
        console.print(f"  {marks.get(t['status'], '○')} {text}")


class HeadlessEvents(Events):
    """For `-p`: answer text on stdout (text format), progress on stderr, approvals denied."""

    def __init__(self, stream_text: bool = False, quiet: bool = False):
        self.stream_text = stream_text
        self.quiet = quiet

    def text(self, delta: str):
        if self.stream_text:
            sys.stdout.write(delta)
            sys.stdout.flush()

    def turn_end(self, result: TurnResult):
        if self.stream_text and result.text and not result.text.endswith("\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()

    def tool_start(self, tool, args):
        if not self.quiet:
            err_console.print(f"[dim]● {TOOL_LABELS.get(tool.name, tool.name)}({escape(describe_call(tool, args))})[/dim]")

    def tool_result(self, tool, args, output, is_error):
        if is_error and not self.quiet:
            err_console.print(f"[red]  ⎿ {escape(output.splitlines()[0][:200] if output else 'error')}[/red]")

    def notice(self, message, level="info"):
        if not self.quiet or level == "error":
            err_console.print(f"[yellow]{escape(message)}[/yellow]")

    def subagent_start(self, key, label):
        if not self.quiet:
            err_console.print(f"[dim]● sub-agent started: {escape(label)}[/dim]")
        self._labels = {**getattr(self, "_labels", {}), key: label}

    def subagent_end(self, key, status, detail=""):
        if not self.quiet:
            icon = {"done": "[green]✔[/green]", "failed": "[red]✘[/red]"}.get(status, "[yellow]■[/yellow]")
            err_console.print(f"  {icon} [dim]{escape(getattr(self, '_labels', {}).get(key, key))}: {escape(detail)}[/dim]")

    def ask(self, tool, args, preview):
        if not self.quiet:
            err_console.print(f"[yellow]  denied (needs approval): {tool.name} {escape(describe_call(tool, args))}. "
                              "Use --permission-mode accept-edits/yolo or allow rules.[/yellow]")
        return "no", ("This action needs user approval, which is unavailable in non-interactive mode. "
                      "Do not retry it; finish what you can and report what remains.")


class StreamJsonEvents(HeadlessEvents):
    """For `-p --output-format stream-json`: one JSON object per line on stdout, as things happen.

    Line types: init, text (a streamed delta), assistant (one finished model call), tool_use,
    tool_result, todos, notice, subagent_start, subagent_end, result (always last).
    """

    def __init__(self, include_deltas: bool = True):
        super().__init__(stream_text=False, quiet=True)
        self.include_deltas = include_deltas

    @staticmethod
    def emit(obj: Dict[str, Any]):
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()

    def text(self, delta):
        if self.include_deltas and delta:
            self.emit({"type": "text", "delta": delta})

    def turn_end(self, result):
        if not (result.text or result.tool_calls):
            return
        self.emit({"type": "assistant", "text": result.text, "finish_reason": result.finish_reason,
                   "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in result.tool_calls],
                   "usage": result.usage})

    def tool_start(self, tool, args):
        self.emit({"type": "tool_use", "tool": tool.name, "args": args})

    def tool_result(self, tool, args, output, is_error):
        self.emit({"type": "tool_result", "tool": tool.name, "is_error": is_error, "output": output[:20000]})

    def todos(self, todos):
        self.emit({"type": "todos", "todos": todos})

    def notice(self, message, level="info"):
        self.emit({"type": "notice", "level": level, "message": message})

    def subagent_start(self, key, label):
        self.emit({"type": "subagent_start", "id": key, "label": label})

    def subagent_end(self, key, status, detail=""):
        self.emit({"type": "subagent_end", "id": key, "status": status, "detail": detail})

    def ask(self, tool, args, preview):
        self.emit({"type": "notice", "level": "warn",
                   "message": f"denied (needs approval): {tool.name} {describe_call(tool, args)}"})
        return super().ask(tool, args, preview)


def _command_family(command: str) -> str:
    """How 'always' is described for a shell command: its prefix, e.g. `pytest` or `git status`."""
    from hubble.permissions import SAFE_COMMAND, command_prefix
    prefix = command_prefix(command)
    if not prefix or not SAFE_COMMAND.match(command.strip()):
        return "this exact command"  # chained/complex commands are only ever remembered verbatim
    return f"`{prefix}` commands"


def _render_choose_items(question: str, options: List[str], selected_idx: int = 0,
                        esc_index: Optional[int] = None) -> Any:
    from prompt_toolkit.formatted_text import FormattedText
    out = [("bold", f" {question}\n\n")]
    for i, opt in enumerate(options):
        sel = i == selected_idx
        prefix = " ❯ " if sel else "   "
        style = "bold #00d7ff" if sel else ""
        out.append((style, f"{prefix}{i + 1}. {opt}"))
        if i == esc_index:
            out.append(("fg:ansigray", "  (esc)\n"))
        else:
            out.append(("", "\n"))
    out.append(("", "\n"))
    out.append(("fg:ansigray", "  Enter to select · 1-9 to choose · Esc to cancel · Ctrl+C to stop"))
    return FormattedText(out)


def choose(question: str, options: List[str], esc_index: Optional[int] = None) -> Optional[int]:
    """A small inline menu like other agent CLIs:

         Run this command?

       ❯ 1. Yes
         2. Yes, and don't ask again for `pytest` commands this session
         3. No, and tell the model what to do instead  (esc)

       Enter to select · 1-9 to choose · Esc to cancel · Ctrl+C to stop

    Enter or 1-9 to choose. Esc picks `esc_index`; Ctrl+C returns
    None (the caller treats it as "stop the turn")."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    state = {"idx": 0}

    def render():
        return _render_choose_items(question, options, state["idx"], esc_index)

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _(event):
        state["idx"] = (state["idx"] - 1) % len(options)

    @kb.add("down")
    @kb.add("c-n")
    @kb.add("tab")
    def _(event):
        state["idx"] = (state["idx"] + 1) % len(options)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=state["idx"])

    for n in range(1, min(len(options), 9) + 1):
        @kb.add(str(n))
        def _(event, n=n):
            event.app.exit(result=n - 1)

    for key, letter_idx in (("y", 0), ("a", 1), ("n", 2)):  # the old one-letter answers still work
        if letter_idx < len(options):
            @kb.add(key)
            def _(event, i=letter_idx):
                event.app.exit(result=i)

    @kb.add("escape", eager=True)
    def _(event):
        event.app.exit(result=esc_index)

    @kb.add("c-c")
    def _(event):
        event.app.exit(result=None)

    app = Application(layout=Layout(Window(FormattedTextControl(render, focusable=True),
                                           dont_extend_height=True)),
                      key_bindings=kb, full_screen=False, erase_when_done=True, mouse_support=False)
    try:
        return app.run()
    except (EOFError, KeyboardInterrupt):
        return None


def ask_line(message: str) -> str:
    """One line of free text (for 'tell the model what to do instead'). Esc/Ctrl+C = empty."""
    from prompt_toolkit import prompt
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings
    kb = KeyBindings()

    @kb.add("escape", eager=True)
    def _(event):
        event.app.exit(result="")

    try:
        return (prompt(HTML(f"<ansigray>{message}</ansigray>"), key_bindings=kb) or "").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def pick(title: str, items: List[Tuple[Any, str, str]], current: Any = None, max_visible: int = 14) -> Any:
    """Inline arrow-key picker. items: (value, label, meta). Returns the chosen value or None.

    Up/Down (or Ctrl+P/N) move, typing filters, Enter selects, Esc or Ctrl+C cancels.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    from prompt_toolkit.key_binding.defaults import load_key_bindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
    from prompt_toolkit.layout.processors import BeforeInput

    if not items:
        return None
    state = {"idx": next((i for i, it in enumerate(items) if it[0] == current), 0)}

    def matches():
        q = query.text.strip().lower()
        return [it for it in items if not q or q in it[1].lower() or q in it[2].lower()]

    def on_change(_):
        state["idx"] = 0

    query = Buffer(multiline=False, on_text_changed=on_change)
    label_width = min(max((len(it[1]) for it in items), default=10) + 2, 60)

    def render():
        rows = matches()
        state["idx"] = max(0, min(state["idx"], len(rows) - 1))
        idx = state["idx"]
        top = max(0, min(idx - max_visible // 2, len(rows) - max_visible))
        out = [("bold", f" {title}\n")]
        if not rows:
            out.append(("fg:ansiyellow", "   no matches\n"))
        for i in range(top, min(top + max_visible, len(rows))):
            value, label, meta = rows[i]
            mark = "●" if value == current else " "
            text = f" {mark} {label:<{label_width}} {meta}"
            if i == idx:
                pal = get_palette(ACTIVE_THEME)
                sel_style = f"bg:{pal.menu_sel_bg} {pal.menu_sel_fg}"
                out.append((sel_style, f"❯{text} \n"))
            else:
                style = "fg:ansigray" if "unavailable" in meta else ""
                out.append((style, f" {text}\n"))
        more = len(rows) - max_visible
        pos = f"{idx + 1}/{len(rows)}" if rows else "0/0"
        out.append(("fg:ansigray", f"  {pos}{'  (scroll for more)' if more > 0 else ''}"
                                   "  ·  ↑↓ move · type to filter · Enter select · Esc cancel"))
        return FormattedText(out)

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    def _(event):
        state["idx"] = max(0, state["idx"] - 1)

    @kb.add("down")
    @kb.add("c-n")
    def _(event):
        state["idx"] = min(len(matches()) - 1, state["idx"] + 1)

    @kb.add("pageup")
    def _(event):
        state["idx"] = max(0, state["idx"] - max_visible)

    @kb.add("pagedown")
    def _(event):
        state["idx"] = min(len(matches()) - 1, state["idx"] + max_visible)

    @kb.add("enter")
    def _(event):
        rows = matches()
        event.app.exit(result=rows[state["idx"]][0] if rows else None)

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=None)

    layout = Layout(HSplit([
        Window(FormattedTextControl(render), dont_extend_height=True),
        Window(BufferControl(query, input_processors=[BeforeInput(" filter: ", style="fg:ansicyan")]), height=1),
    ]), focused_element=query)
    app = Application(layout=layout, key_bindings=merge_key_bindings([load_key_bindings(), kb]),
                      full_screen=False, erase_when_done=True, mouse_support=False)
    try:
        return app.run()
    except (EOFError, KeyboardInterrupt):
        return None
