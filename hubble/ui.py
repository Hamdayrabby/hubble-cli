"""Terminal rendering: streamed markdown, tool call lines, diffs and approval prompts."""

import json
import sys
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from hubble.agent import Events, describe_call
from hubble.provider import TurnResult
from hubble.board import SubagentBoard
from hubble.spinner import start_shimmer
from hubble.tools import Tool, ToolContext

if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

TOOL_LABELS = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit", "shell": "Shell",
               "grep": "Search", "glob": "Glob", "list_dir": "List", "todo_write": "Todos", "task": "Task",
               "web_search": "Web Search", "web_fetch": "Fetch"}
MAX_DIFF_LINES = 120


def render_diff(diff: str, max_lines: int = MAX_DIFF_LINES) -> Text:
    out = Text()
    lines = diff.splitlines()
    for line in lines[:max_lines]:
        if line.startswith(("+++", "---")):
            style = "bold"
        elif line.startswith("+"):
            style = "green"
        elif line.startswith("-"):
            style = "red"
        elif line.startswith("@@"):
            style = "cyan"
        else:
            style = "dim"
        out.append(line + "\n", style=style)
    if len(lines) > max_lines:
        out.append(f"... {len(lines) - max_lines} more diff lines\n", style="dim")
    return out


class _BlockPreview:
    """The block still being written, redrawn by rich.live on each refresh (~12/s)."""

    def __init__(self, owner: "StreamingMarkdown"):
        self.owner = owner

    def __rich__(self):
        text = self.owner.buf
        if not text.strip():
            return Text("")
        lines = text.splitlines()
        room = max(4, self.owner.console.height - 4)
        if len(lines) > room:
            # Taller than the screen: show the newest lines as plain text; the whole block is
            # printed properly formatted the moment it is complete.
            return Text("\n".join(lines[-room:]))
        return Markdown(text, code_theme="monokai")


class StreamingMarkdown:
    """Renders markdown as it streams. Finished blocks are printed for good (formatted, never
    redrawn); the block still being written shows live underneath them, so text appears as soon
    as the model sends it instead of only when a paragraph or code block is complete."""

    def __init__(self, con: Console):
        self.console = con
        self.buf = ""
        self.printed_any = False
        self.live = None

    def feed(self, delta: str):
        self.buf += delta
        cut = self._boundary()
        if cut:
            chunk, self.buf = self.buf[:cut], self.buf[cut:]
            self._render(chunk)
        if self.live is None and self.buf.strip() and self.console.is_terminal:
            from rich.live import Live
            from hubble.spinner import Footed
            self.live = Live(Footed(_BlockPreview(self)), console=self.console, refresh_per_second=12,
                             transient=True, vertical_overflow="crop")
            self.live.start()

    def _stop_live(self):
        if self.live is not None:
            try:
                self.live.stop()
            finally:
                self.live = None

    def flush(self):
        self._stop_live()
        if self.buf.strip():
            self._render(self.buf)
        self.buf = ""

    def _boundary(self) -> Optional[int]:
        in_fence, pos, last = False, 0, None
        for line in self.buf.splitlines(keepends=True):
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
        if self.printed_any:
            self.console.print()
        self.console.print(Markdown(chunk.strip("\n"), code_theme="monokai"))
        self.printed_any = True


def activity_label(tool: Tool, args: Dict[str, Any]) -> str:
    """Words shown in the shimmer while a tool runs."""
    target = describe_call(tool, args)
    short = target if len(target) <= 48 else target[:45] + "..."
    return {
        "read_file": f"Reading {short}", "write_file": f"Writing {short}", "edit_file": f"Editing {short}",
        "shell": f"Running {short}", "grep": f"Searching for {short}", "glob": f"Finding {short}",
        "list_dir": f"Looking around {short}", "todo_write": "Updating the plan", "task": f"Researching {short}",
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
    if tool.name == "task":
        return f"Report ready ({len(output):,} chars)", lines[:2]
    return lines[0][:200] if lines else "done", []


class ReplEvents(Events):
    def __init__(self, ctx: ToolContext, show_reasoning: bool = False):
        self.ctx = ctx
        self.show_reasoning = show_reasoning
        self.reasoning_chars = 0
        self.answer_started = False
        self.md: Optional[StreamingMarkdown] = None
        self.status = None
        self.in_reasoning = False
        self.approved_diff_shown = False
        self.batch = 0  # >0 while parallel sub-agents run: the board owns the screen
        self.board: Optional[SubagentBoard] = None
        self.board_live = None

    # ----- sub-agent dashboard ---------------------------------------------

    def batch_start(self, count: int):
        self._stop_status(force=True)
        self.batch = count

    def batch_end(self):
        self.batch = 0
        self._close_board()

    def subagent_start(self, key: str, label: str):
        if self.board is None:
            from rich.live import Live
            self._stop_status(force=True)
            from hubble.spinner import Footed
            self.board = SubagentBoard()
            self.board_live = Live(Footed(self.board), console=console, refresh_per_second=12, transient=True)
            self.board_live.start()
        self.board.add(key, label)

    def subagent_step(self, key: str, action: str, is_tool: bool = True):
        if self.board:
            self.board.step(key, action, is_tool)

    def subagent_tokens(self, key: str, tokens: int):
        if self.board:
            self.board.tokens(key, tokens)

    def subagent_end(self, key: str, status: str, detail: str = ""):
        if self.board:
            self.board.finish(key, status, detail)

    def _close_board(self):
        """Stop the live board and leave its final state on screen."""
        if self.board_live is not None:
            self.board_live.stop()
            console.print(self.board)
            self.board_live = None
            self.board = None

    def _stop_status(self, force: bool = False):
        if (self.batch or self.board_live is not None) and not force:
            return
        if self.status is not None:
            self.status.stop()
            self.status = None

    def _end_reasoning(self):
        if self.in_reasoning:
            console.print()
            self.in_reasoning = False

    def turn_start(self):
        self.md = StreamingMarkdown(console)
        self.reasoning_chars = 0
        self.answer_started = False
        self.status = start_shimmer(console)

    def reasoning(self, delta: str):
        if not self.show_reasoning:
            # Keep the animation running and count the hidden thinking tokens (/thinking shows them).
            self.reasoning_chars += len(delta)
            if self.status is not None and hasattr(self.status, "shimmer"):
                self.status.shimmer.tokens = int(self.reasoning_chars / 3.8)
            return
        self._stop_status()
        if not self.in_reasoning:
            console.print("[dim italic]* thinking[/dim italic]")
            self.in_reasoning = True
        console.print(Text(delta, style="dim italic"), end="")

    def text(self, delta: str):
        self._stop_status()
        self._end_reasoning()
        if not self.answer_started:
            # A clear header, distinct from the cyan "you typed this" prompt line above it.
            console.print("[bold #af87ff]✻ Hubble[/bold #af87ff]")
            self.answer_started = True
        if self.md:
            self.md.feed(delta)

    def turn_end(self, result: TurnResult):
        self._stop_status()
        self._end_reasoning()
        if self.md:
            self.md.flush()
            if self.md.printed_any:
                console.print()

    def tool_start(self, tool: Tool, args: Dict[str, Any]):
        label = TOOL_LABELS.get(tool.name, tool.name)
        if tool.name == "task":
            return  # shown on the sub-agent board instead
        console.print(f"[bold cyan]●[/bold cyan] [bold]{label}[/bold]([cyan]{escape(describe_call(tool, args))}[/cyan])")
        if tool.kind == "edit" and not self.approved_diff_shown:
            try:
                diff = tool.preview(args, self.ctx)
            except Exception:
                diff = None
            if diff and diff.startswith("---"):
                console.print(render_diff(diff, max_lines=40))
        self.approved_diff_shown = False
        if not self.batch:
            self.status = start_shimmer(console, activity_label(tool, args))

    def tool_result(self, tool: Tool, args: Dict[str, Any], output: str, is_error: bool):
        if tool.name == "task" and args:
            if not self.batch:
                self._close_board()  # single sub-agent: its board ends with it
            return
        self._stop_status()
        if not args and is_error:
            console.print(f"[bold red]●[/bold red] [bold]{TOOL_LABELS.get(tool.name, tool.name)}[/bold]")
        headline, extra = summarize_output(tool, output, is_error)
        if tool.name == "task" and args.get("description"):
            headline = f"{args['description']}: {headline}"
        style = "red" if is_error else "dim"
        console.print(f"  [dim]⎿[/dim]  [{style}]{escape(headline)}[/{style}]")
        for line in extra:
            console.print(f"     [dim]{escape(line[:160])}[/dim]")

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
            return self._ask(tool, args, preview)
        finally:
            if paused and self.board_live is not None:
                self.board_live.start()

    def _ask(self, tool: Tool, args: Dict[str, Any], preview: Optional[str]) -> Tuple[str, str]:
        label = TOOL_LABELS.get(tool.name, tool.name)
        title = f"{label} {describe_call(tool, args)}"
        if tool.kind == "exec":
            body: Any = Text(preview or args.get("command", ""), style="bold")
            if args.get("description"):
                body = Text.assemble(body, "\n", Text(args["description"], style="dim"))
        elif preview and preview.startswith("---"):
            body = render_diff(preview)
        else:
            body = Text(preview or "", style="dim")
        console.print(Panel(body, title=f"[bold yellow]{escape(title[:100])}[/bold yellow]",
                            title_align="left", border_style="yellow"))
        always = {"edit": "edits", "web": tool.target(args), "mcp": tool.target(args)}.get(
            tool.kind, _command_family(args.get("command", "")))
        question = {"exec": "Run this command?", "edit": "Make this change?", "web": "Fetch this page?"}.get(
            tool.kind, f"Allow {label}?")
        if sys.stdin.isatty() and console.is_terminal:
            choice = choose(question, ["Yes", f"Yes, and don't ask again for {always} this session",
                                       "No, and tell the model what to do instead"], esc_index=2)
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
            feedback = ask_line("  Tell the model what to do instead (Enter to just say no): ")
            console.print("  [red]✘[/red] [dim]denied" + (f": {escape(feedback)}" if feedback else "") + "[/dim]")
            return "no", feedback
        # Not an interactive terminal (piped input): the plain typed answer.
        console.print(f"  [bold]y[/bold] yes   [bold]a[/bold] always ({escape(always)} this session)   "
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
            if answer in ("n", "no"):
                try:
                    feedback = console.input("[dim]  feedback for the model (optional): [/dim]").strip()
                except EOFError:
                    feedback = ""
                return "no", feedback
            if answer.startswith("n "):
                return "no", answer[2:].strip()
            console.print("[dim]  type y, a or n[/dim]")


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

    def __init__(self, stream_text: bool, quiet: bool = False):
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


def choose(question: str, options: List[str], esc_index: Optional[int] = None) -> Optional[int]:
    """A small inline menu like other agent CLIs:

         Run this command?
       ❯ 1. Yes
         2. Yes, and don't ask again for `pytest` commands this session
         3. No, and tell the model what to do instead  (esc)

    ↑/↓ then Enter, or press the number to answer at once. Esc picks `esc_index`; Ctrl+C returns
    None (the caller treats it as "stop the turn")."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    state = {"idx": 0}

    def render():
        out = [("bold", f" {question}\n")]
        for i, opt in enumerate(options):
            sel = i == state["idx"]
            out.append(("fg:#00d7ff bold" if sel else "", f" {'❯' if sel else ' '} {i + 1}. {opt}"))
            out.append(("fg:ansigray", "  (esc)\n" if i == esc_index else "\n"))
        out.append(("fg:ansigray", "   ↑↓ move · Enter or 1-9 choose · Ctrl+C stop the turn"))
        return FormattedText(out)

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
                out.append(("reverse bold", f"❯{text} \n"))
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
