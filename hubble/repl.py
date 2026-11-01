"""Interactive REPL: prompt_toolkit input, slash commands, @file mentions, # memory notes."""

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import completion_is_selected
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.markup import escape
from rich.table import Table

from hubble import __version__
from hubble.agent import Agent
from hubble.images import ImageError, content_text, data_url, grab_clipboard_image, is_image
from hubble.models import resolve_model
from hubble.provider import OpenAICompatProvider
from hubble.providers import (DEFAULT_PROVIDER, ProviderConfig, normalize_provider_name, provider_models,
                                remove_provider, resolve_fallback, save_listing, save_provider, scan_file)
from hubble.permissions import MODE_HELP, MODES
from hubble.prompts import INIT_PROMPT, PERSONAS
from hubble.scanner import ModelScanner, scan_age_hours
from hubble.session import SessionStore
from hubble.settings import HOME_DIR, save_user_setting
from hubble.skills import Skill, TEMPLATE, discover_skills
from hubble.tools import is_secret_path, truncate
from hubble.theme import CONTENT_MAX
from hubble.ui import console, pick, render_todos

MENTION_RX = re.compile(r"(?<![\w@])@([\w./\\:-]+)")
MAX_MENTION_CHARS = 60000


class Command:
    def __init__(self, name: str, fn: Callable, help: str, aliases: Tuple[str, ...] = (), args: str = ""):
        self.name, self.fn, self.help, self.aliases, self.args = name, fn, help, aliases, args


def _is_subsequence(query: str, target: str) -> bool:
    it = iter(target.lower())
    return all(c in it for c in query.lower())


def _segmented_gauge(ratio: float, blocks: int = 6) -> str:
    ratio = max(0.0, min(1.0, ratio))
    filled = int(round(ratio * blocks))
    return "█" * filled + "░" * (blocks - filled)


def _indent_frags(frags: List[Tuple[str, str]], margin: int, style: str = "") -> List[Tuple[str, str]]:
    """Shift every rendered row right by `margin` columns, so multi-row blocks (the home
    screen, the sub-agent board) center on the same column as the prompt panel. `style`
    colors that margin (the home canvas paints it with the page background)."""
    if margin <= 0:
        return frags
    out: List[Tuple[str, str]] = []
    need = True                       # at the start of a row
    for style_, text in frags:
        while text:
            i = text.find("\n")
            if i < 0:
                if need:
                    out.append((style, " " * margin))
                    need = False
                out.append((style_, text))
                text = ""
            else:
                head, text = text[:i], text[i + 1:]
                if head:
                    if need:
                        out.append((style, " " * margin))
                        need = False
                    out.append((style_, head))
                out.append(("", "\n"))
                need = True
    return out


def _estimate_session_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    m = model.lower()
    if "claude-3-5-sonnet" in m or "claude-3-7-sonnet" in m:
        p_rate, c_rate = 3.0, 15.0
    elif "claude-3-opus" in m:
        p_rate, c_rate = 15.0, 75.0
    elif "claude-3-5-haiku" in m:
        p_rate, c_rate = 0.80, 4.0
    elif "gpt-4o-mini" in m:
        p_rate, c_rate = 0.15, 0.60
    elif "gpt-4o" in m:
        p_rate, c_rate = 2.50, 10.0
    elif "gemini-2" in m or "gemini-1.5-flash" in m:
        p_rate, c_rate = 0.10, 0.40
    elif "gemini-1.5-pro" in m:
        p_rate, c_rate = 1.25, 5.0
    elif "deepseek" in m or "qwen" in m or "codestral" in m:
        p_rate, c_rate = 0.25, 1.00
    else:
        p_rate, c_rate = 1.0, 3.0
    return (prompt_tokens * p_rate + completion_tokens * c_rate) / 1_000_000.0


class ReplCompleter(Completer):
    def __init__(self, repl: "Repl"):
        self.repl = repl

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        m = re.match(r"^/model\s+(\S*)$", text)
        if m:
            typed = m.group(1).lower()
            current = (self.repl.agent.provider_name, self.repl.agent.model)
            for value, label, meta in self.repl.model_items():
                if typed in label.lower():
                    mark = " (current)" if value == current else ""
                    yield Completion(label, start_position=-len(m.group(1)), display_meta=meta.strip() + mark)
            return

        # Subcommand autocompletions for /route, /provider, /mode, /theme, /worktree, /thinking, /multiline, /team
        m_cmd = re.match(r"^/(\w[\w-]*)\s+(\S*)$", text)
        if m_cmd:
            cname = m_cmd.group(1).lower()
            arg_typed = m_cmd.group(2).lower()
            pos = -len(m_cmd.group(2))
            if cname in ("route", "routing"):
                opts = [("fast ", "Set fast model for simple queries"),
                        ("strong ", "Set strong model for hard queries"),
                        ("auto", "Enable smart query routing"),
                        ("on", "Enable smart query routing"),
                        ("off", "Disable smart query routing")]
                for opt, desc in opts:
                    if arg_typed in opt.lower():
                        yield Completion(opt, start_position=pos, display_meta=desc)
                return
            if cname in ("provider", "providers"):
                opts = [("list", "List configured providers"),
                        ("add ", "Add a new model provider"),
                        ("remove ", "Remove a provider"),
                        ("secure", "Encrypt stored provider API keys")]
                for opt, desc in opts:
                    if arg_typed in opt.lower():
                        yield Completion(opt, start_position=pos, display_meta=desc)
                return
            if cname in ("mode", "permissions"):
                from hubble.permissions import MODES
                for mode_name in MODES:
                    if arg_typed in mode_name.lower():
                        yield Completion(mode_name, start_position=pos, display_meta="Permission tier")
                return
            if cname in ("theme", "color", "colors"):
                from hubble.theme import COLOR_SCHEMES
                for th_name, th_obj in COLOR_SCHEMES.items():
                    if arg_typed in th_name.lower():
                        yield Completion(th_name, start_position=pos, display_meta=th_obj.description)
                return
            if cname in ("worktree", "worktrees", "wt"):
                opts = [("new ", "Create new isolated git worktree"),
                        ("switch ", "Switch to an existing worktree"),
                        ("exit", "Return to primary root workspace"),
                        ("list", "List active worktrees"),
                        ("remove ", "Clean up a worktree branch")]
                for opt, desc in opts:
                    if arg_typed in opt.lower():
                        yield Completion(opt, start_position=pos, display_meta=desc)
                return
            if cname == "thinking":
                opts = [("view", "Inspect reasoning / chain of thought from last turn"),
                        ("on", "Show live thinking preview accordion"),
                        ("off", "Hide live thinking preview")]
                for opt, desc in opts:
                    if arg_typed in opt.lower():
                        yield Completion(opt, start_position=pos, display_meta=desc)
                return
            if cname == "multiline":
                opts = [("on", "Enter adds newline, Esc+Enter submits"),
                        ("off", "Enter submits, Shift+Enter adds newline")]
                for opt, desc in opts:
                    if arg_typed in opt.lower():
                        yield Completion(opt, start_position=pos, display_meta=desc)
                return
            if cname == "team":
                opts = [("add ", "Add model to subagent pool"),
                        ("remove ", "Remove model by index"),
                        ("clear", "Reset subagent pool to default")]
                for opt, desc in opts:
                    if arg_typed in opt.lower():
                        yield Completion(opt, start_position=pos, display_meta=desc)
                return

        if text.startswith("/") and " " not in text:
            typed = text[1:].lower()
            for cmd in self.repl.commands.values():
                names = [cmd.name, *cmd.aliases]
                hit = next((n for n in names if n.startswith(typed)), None)
                if hit is None:
                    continue
                shown = f"/{cmd.name} {cmd.args}".rstrip()
                yield Completion("/" + (cmd.name if hit == cmd.name or not typed else hit),
                                 start_position=-len(text), display=shown, display_meta=cmd.help)
            return

        m = re.search(r"@([\w./\\-]*)$", text)
        if m:
            partial = m.group(1).replace("\\", "/")
            pos = -len(m.group(1))

            # Semantic pseudo-file completions:
            semantic = [
                ("git", "Git status, branch, and staged file list"),
                ("diff", "Current uncommitted working directory git diff"),
                ("todos", "Active project task list and todos"),
                ("problems", "Recent errors and diagnostics"),
            ]
            for tag, meta in semantic:
                if not partial or _is_subsequence(partial, tag):
                    yield Completion(tag, start_position=pos, display=f"@{tag}", display_meta=meta)

            # Filesystem completions (fuzzy subsequence):
            root = self.repl.agent.ctx.root
            if "/" in partial:
                base_dir = root / os.path.dirname(partial)
                prefix = os.path.basename(partial)
                try:
                    entries = sorted(base_dir.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
                except OSError:
                    return
                for e in entries[:200]:
                    if e.name.startswith(prefix) and e.name not in (".git", "__pycache__", "node_modules", ".venv", "venv"):
                        rel = (Path(os.path.dirname(partial)) / e.name).as_posix().removeprefix("./")
                        yield Completion(rel + ("/" if e.is_dir() else ""), start_position=pos)
            else:
                try:
                    candidates = []
                    for e in sorted(root.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                        if e.name in (".git", "__pycache__", "node_modules", ".venv", "venv"):
                            continue
                        candidates.append(e)
                        if e.is_dir() and e.name in ("tests", "hubble", "src", "docs"):
                            try:
                                for sub in sorted(e.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                                    if sub.name not in (".git", "__pycache__"):
                                        candidates.append(sub)
                            except OSError:
                                pass
                    for cand in candidates[:300]:
                        rel = cand.relative_to(root).as_posix()
                        if not partial or _is_subsequence(partial, rel):
                            is_dir = cand.is_dir()
                            yield Completion(rel + ("/" if is_dir else ""), start_position=pos,
                                             display=f"{cand.name}{'/' if is_dir else ''}",
                                             display_meta=rel if rel != cand.name else "")
                except OSError:
                    pass
            return


class _QueuedPrompt(str):
    """A queued prompt that carries the clipboard images pasted with it.

    Subclasses str so /queue, /clear-queue and anything else that only reads the text keep
    working; Repl.send reads `images` so a later paste cannot be consumed by an earlier
    queued message.
    """

    images: List[Path]

    def __new__(cls, text: str, images: Optional[List[Path]] = None):
        prompt = super().__new__(cls, text)
        prompt.images = list(images or [])
        return prompt


class Repl:
    def __init__(self, agent: Agent, store: SessionStore, providers: Optional[Dict[str, ProviderConfig]] = None):
        self.providers: Dict[str, ProviderConfig] = dict(providers or {})
        self.clients: Dict[str, OpenAICompatProvider] = {agent.provider_name: agent.provider}
        # Lets routing (and routed sub-agents) switch to a model on another configured provider.
        agent.client_for = lambda name: self.client(name) if name in self.providers else None
        self.scanners: Dict[str, ModelScanner] = {}
        self._unannounced: set = set()
        self._quiet_scans: set = set()  # startup scans: say nothing unless they fail
        self._scan_key = None           # last seen scanner progress, so redraws only rebuild on change
        self._home_info_cache = None
        self._home = None               # home screen (status, tips) while the first prompt is showing
        self._pending_images: List[Path] = []  # pasted from the clipboard, sent with the next message
        self._main_root: Optional[Path] = None  # set once /worktree moves the tools elsewhere
        from hubble.update import UpdateChecker
        self.updates = UpdateChecker(enabled=bool(agent.settings.get("update_check", True)))
        self._update_scheduled = False
        self._last_ctrl_c = 0.0
        self.turn_stats = None
        self.message_queue: List[str] = []
        self.is_busy: bool = False
        self._worker_thread: Optional[threading.Thread] = None
        self._task_queue: queue.Queue = queue.Queue()
        self._shutdown_event = threading.Event()
        self._APPROVAL_SENTINEL = object()
        self._pending_approval = None
        self.agent = agent
        agent.fallback_resolver = self.fallback_for
        self.store = store
        self.last_model_list: List[str] = []
        self.commands: Dict[str, Command] = {}
        self._register_builtin()
        self._load_custom_commands()
        self._register_mcp_prompts()

    # ----- setup ---------------------------------------------------------

    def _register(self, name, fn, help, aliases=(), args=""):
        self.commands[name] = Command(name, fn, help, aliases, args)

    def command_names(self) -> Dict[str, Command]:
        names = {}
        for cmd in self.commands.values():
            names[cmd.name] = cmd
            for a in cmd.aliases:
                names[a] = cmd
        return names

    def _register_builtin(self):
        r = self._register
        r("help", self.cmd_help, "Show commands and shortcuts", ("h", "?"))
        r("home", lambda arg: self.banner(), "Show the animated home screen again", ("intro", "banner"))
        r("exit", self.cmd_exit, "Quit", ("quit", "q"))
        r("clear", self.cmd_clear, "Start a new conversation (new session)", ("new", "reset"))
        r("compact", self.cmd_compact, "Summarize history to free context", args="[focus]")
        r("model", self.cmd_model, "Show or switch model (name, fragment or # from /models)", args="[name|#]")
        r("models", self.cmd_models, "List models; '/models refresh' re-checks which are available",
          args="[filter|refresh]")
        r("provider", self.cmd_provider, "Switch provider, or add/remove one (base URL + API key)",
          ("providers",), "[add|remove <name>|list|secure]")
        r("mode", self.cmd_mode, "Permission mode: default, accept-edits, plan, yolo (Shift+Tab cycles)",
          ("permissions",), "[mode]")
        r("theme", self.cmd_theme, "Choose or switch color scheme (interactive picker or name)",
          ("color", "colors", "color-scheme"), "[name]")
        r("logo", self.cmd_logo, "Choose logo style: regular (clean bold wordmark), block (classic block letters), or small",
          ("banner-logo",), "[regular|block|small]")
        r("persona", self.cmd_persona, f"System persona: {', '.join(PERSONAS)}", args="[name]")
        r("resume", self.cmd_resume, "Resume a previous session of this project", ("sessions",), "[id|#]")
        r("cost", self.cmd_cost, "Token usage and context size of this session", ("tokens", "status"))
        r("team", self.cmd_team, "Choose the models sub-agents can use (the main model picks one per task)",
          args="[add <provider:model> [use] | remove <#> | clear]")
        r("stats", self.cmd_stats, "Which models finish your tasks, how fast, at what cost", args="[days]")
        r("route", self.cmd_route, "Route easy prompts to a fast model and hard ones to a strong model",
          ("routing",), "[fast <model> | strong <model> | auto | off | on]")
        r("context", self.cmd_context, "Show what is in the context window")
        r("undo", self.cmd_undo, "Revert file changes from the last turn that edited files", ("rewind",))
        r("diff", self.cmd_diff, "Show git diff of the workspace")
        r("init", self.cmd_init, "Generate HUBBLE.md project instructions")
        r("memory", self.cmd_memory, "Show loaded memory files (HUBBLE.md / AGENTS.md)")
        r("lens", self.cmd_lens, "Show, init or configure developer lens & design language", ("style", "taste"), "[init|add <rule>]")
        r("add", self.cmd_add, "Pin a file into the system prompt", ("pin",), "<file>")
        r("drop", self.cmd_drop, "Unpin a file", ("unpin",), "<file>")
        r("files", self.cmd_files, "List pinned files")
        r("todos", self.cmd_todos, "Show the current task list")
        r("test", self.cmd_test, "Check a model responds and measure latency", args="[model]")
        r("temp", self.cmd_temp, "Show or set temperature", args="[0-2]")
        r("fallback", self.cmd_fallback, "Choose the model used when the current one is rate limited",
          args="[model|auto|off]")
        r("thinking", self.cmd_thinking, "Show, hide or view the model's reasoning text", args="[on|off|view]")
        r("multiline", self.cmd_multiline, "Toggle multiline input mode (Enter newlines vs submits)", args="[on|off]")
        r("sandbox", self.cmd_sandbox, "Sandbox shell commands: OS sandbox (macOS/Linux), Docker, or off",
          args="[auto|native|docker|off]")
        r("mcp", self.cmd_mcp, "List MCP servers; log in to or out of a remote one", args="[login|logout <server>]")
        r("hooks", self.cmd_hooks, "List configured hooks")
        r("config", self.cmd_config, "Show effective settings and allow/deny rules")
        r("export", self.cmd_export, "Export conversation to markdown", ("copy",), "[file]")
        r("skills", self.cmd_skills, "List skills (the model can also call these on its own)")
        r("skill", self.cmd_skill_new, "Create a new skill template to fill in", args="new <name> [user]")
        r("install-github", self.cmd_install_github, "Set up the GitHub Action: PR reviews and @hubble in comments",
          ("github",), "[--force]")
        r("worktree", self.cmd_worktree, "Work in an isolated git worktree (new, switch, exit, remove, list)",
          ("worktrees", "wt"), "[new|switch|exit|remove <name>]")
        r("orchestrate", self.cmd_orchestrate, "Run an external coding runtime in an isolated worktree",
          ("orch",), "[runtimes | run <id>:<objective> [--runtime <name>]]"),
        r("agent", self.cmd_agents, "List specialist agents, or create/fork one", ("agents",),
          "[new|create|add|fork <name>]")
        r("plugin", self.cmd_plugin, "List, install, remove, enable or disable plugins", ("plugins",),
          "[list|install <src>|remove <name>]")
        r("subagent", self.cmd_subagent, "Control sub-agent policy: auto, explicit, or off", ("subagents",),
          "[auto|explicit|off]")
        r("queue", self.cmd_queue, "List pending queued user prompts")
        r("clear-queue", self.cmd_clear_queue, "Clear all pending queued user prompts", ("cq",))
        r("update", self.cmd_update, "Check for updates and upgrade Hubble", ("upgrade",))

    def _load_custom_commands(self):
        """Skills become /<name>; a skill's own description is shown in /help and /<tab>."""
        for skill in self.agent.skills:
            self._register(skill.name, self._make_skill_runner(skill),
                           f"(skill, {skill.scope}) {skill.description}", args=skill.args_hint or "[args]")
        # Back-compat: a plain command with no frontmatter, e.g. from an older .hubble/commands/ setup.
        from hubble.plugins import plugin_dirs
        folders = ([HOME_DIR / "commands"] + [d / "commands" for _, d in plugin_dirs(self.agent.ctx.root)]
                   + [self.agent.ctx.root / ".hubble" / "commands"])
        for folder in folders:
            if not folder.is_dir():
                continue
            for f in sorted(folder.glob("*.md")):
                name = f.stem.lower()
                if name in self.commands:
                    continue
                try:
                    body = f.read_text(encoding="utf-8")
                except OSError:
                    continue
                first = next((l.strip() for l in body.splitlines() if l.strip()), "")[:60]

                def run(arg, body=body):
                    prompt = body.replace("$ARGUMENTS", arg) if "$ARGUMENTS" in body else \
                        (body + (f"\n\n{arg}" if arg else ""))
                    self.send(prompt)
                self._register(name, run, f"(custom) {first}", args="[args]")

    def _make_skill_runner(self, skill: Skill):
        def run(arg):
            body = skill.body()
            prompt = body.replace("$ARGUMENTS", arg) if "$ARGUMENTS" in body else \
                (body + (f"\n\n{arg}" if arg else ""))
            self.send(prompt)
        return run

    # ----- main loop -----------------------------------------------------

    def _prompt_session(self) -> PromptSession:
        kb = KeyBindings()

        try:
            from prompt_toolkit.input.vt100_parser import ANSI_SEQUENCES
            ANSI_SEQUENCES["\x1b[13;2u"] = Keys.ControlJ
            ANSI_SEQUENCES["\x1b[27;2;13~"] = Keys.ControlJ
        except Exception:
            pass

        @kb.add("escape", "enter")
        def _(event):
            buf = event.current_buffer
            if getattr(self, "multiline_mode", False):
                buf.validate_and_handle()
            else:
                buf.insert_text("\n")

        @kb.add("escape", "v")  # Alt+V; works in every terminal, including Windows Terminal
        @kb.add("c-v")          # Ctrl+V where the terminal forwards it (macOS/Linux); terminals
                                # that keep Ctrl+V for their own text paste never send it to us
        def _(event):
            placeholder = self.paste_image()
            if placeholder:
                event.current_buffer.insert_text(placeholder + " ")
            else:
                from prompt_toolkit.application import run_in_terminal
                run_in_terminal(lambda: console.print("[dim]No image on the clipboard.[/dim]"))

        @kb.add(Keys.BracketedPaste)  # Windows Terminal sends empty bracketed paste when clipboard has only an image
        def _(event):
            if event.data == "":  # empty paste = likely image-only clipboard
                placeholder = self.paste_image()
                if placeholder:
                    event.current_buffer.insert_text(placeholder + " ")
                else:
                    from prompt_toolkit.application import run_in_terminal
                    run_in_terminal(lambda: console.print("[dim]No image on the clipboard.[/dim]"))
            else:
                # Normal text paste: let the default handler insert it
                event.current_buffer.insert_text(event.data.replace("\r\n", "\n").replace("\r", "\n"))

        @kb.add("c-j")
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add("c-c")
        def _(event):
            # Ctrl+C with text typed clears the line; on an empty line it is the first half of
            # "press Ctrl+C twice to exit".
            buf = event.current_buffer
            if buf.text:
                buf.reset()
            else:
                event.app.exit(exception=KeyboardInterrupt)

        @kb.add("s-tab")
        def _(event):
            self.agent.permissions.cycle_mode()
            event.app.invalidate()

        # Completion menu navigation: highlight-only. prompt_toolkit's stock DOWN/UP
        # rewrite the buffer with every move (tab-cycle preview); here the menu picks
        # on Enter instead, so the arrows just move the selection.
        from prompt_toolkit.filters import has_completions

        @kb.add("down", filter=has_completions)
        def _(event):
            st = event.current_buffer.complete_state
            if not (st and st.completions):
                return
            idx = st.complete_index
            st.go_to_index(0 if idx is None else min(idx + 1, len(st.completions) - 1))

        @kb.add("up", filter=has_completions)
        def _(event):
            st = event.current_buffer.complete_state
            if not (st and st.completions):
                return
            idx = st.complete_index
            st.go_to_index(len(st.completions) - 1 if idx is None else max(idx - 1, 0))

        @kb.add("enter", filter=completion_is_selected)
        def _(event):
            # A highlighted item in the dropdown (e.g. /model <space>, @file): one Enter both
            # picks it and submits, instead of submitting the still-unfinished typed text.
            buf = event.current_buffer
            buf.apply_completion(buf.complete_state.current_completion)
            buf.validate_and_handle()

        @kb.add("enter", filter=~completion_is_selected)
        def _(event):
            buf = event.current_buffer
            text = buf.text
            # Auto-multiline detection for unclosed codeblocks
            if text.count("```") % 2 != 0:
                buf.insert_text("\n")
                return
            if getattr(self, "multiline_mode", False):
                buf.insert_text("\n")
                return
            buf.validate_and_handle()

        HOME_DIR.mkdir(parents=True, exist_ok=True)
        from prompt_toolkit.styles import Style
        from hubble.theme import get_prompt_style
        theme_name = self.agent.settings.get("theme", "terminal")
        self.session = PromptSession(history=FileHistory(str(HOME_DIR / "history")), completer=ReplCompleter(self),
                                     complete_while_typing=True, key_bindings=kb, bottom_toolbar=self._toolbar,
                                     multiline=False, enable_history_search=False,
                                     input_processors=[self._panel_fill()],
                                     placeholder=self._placeholder_text, prompt_continuation=self._bar_continuation,
                                     refresh_interval=0.1 if getattr(self, "is_busy", False) else 1.0,
                                     reserve_space_for_menu=0, style=Style.from_dict(get_prompt_style(theme_name)))
        # Auto-select the closest option: every time the completer produces matches, the
        # top row is pre-highlighted, so the menu always shows the closest match selected
        # and Enter runs it without touching the arrow keys (OpenCode-style).
        # go_to_index only sets the index — no event re-fire, no loop.
        self.session.default_buffer.on_completions_changed += self._autoselect_first
        # OpenCode-style prompt box: the default buffer window has an unbounded height, so the
        # root HSplit hands it every leftover screen row — the typed line stays at the top
        # while the metadata row and footer are pushed to the screen bottom, splitting the
        # box. Capping that one window at the buffer's own preferred (wrap-aware) height
        # keeps the input row, metadata row and footer on one adjacent surface.
        from prompt_toolkit.filters import Always
        from prompt_toolkit.layout.controls import BufferControl

        for window in self.session.layout.find_all_windows():
            control = window.content
            if isinstance(control, BufferControl) and control.buffer is self.session.default_buffer:
                window.dont_extend_height = Always()
                break

        # The pondering line hovers ABOVE the box while a turn runs (Claude Code layout):
        # the committed prompt scrolls into the transcript above it, this animated row
        # plays below it, and the input panel sits at the bottom. The extra fixed-height
        # spacer row between the transcript and the panel gives both breathing room —
        # fixed dimensions (no weight/weight fillers) keep the renderer calm.
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.layout.containers import ConditionalContainer, HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension

        root_layout = self.session.layout.container
        if isinstance(root_layout, HSplit):
            thinking = ConditionalContainer(
                Window(FormattedTextControl(self._thinking_screen, show_cursor=False),
                       height=Dimension.exact(1), always_hide_cursor=True, dont_extend_height=True),
                filter=Condition(lambda: bool(getattr(self, "is_busy", False))),
            )
            gap = Window(height=Dimension.exact(1), char=" ")   # blank row: transcript ↔ panel
            root_layout.children.insert(0, thinking)
            root_layout.children.insert(0, gap)

        # The renderer grows the prompt region to all the rows below the cursor, so on a
        # roomy terminal the input row would sit at the top of a tall empty region with the
        # footer pinned to the screen bottom. Cap that height at the layout's preferred
        # height, so the box stays compact and finished output scrolls above it.
        renderer = self.session.app.renderer

        def _cap_prompt_height() -> None:
            try:
                size = renderer.output.get_size()
                preferred = self.session.layout.container.preferred_height(
                    size.columns, size.rows
                ).preferred
            except Exception:
                return
            if renderer._min_available_height > preferred:
                renderer._min_available_height = preferred

        request_position = renderer.request_absolute_cursor_position

        def _capped_request() -> None:
            request_position()
            _cap_prompt_height()

        report_row = renderer.report_absolute_cursor_row

        def _capped_report(row: int) -> None:
            report_row(row)
            _cap_prompt_height()

        renderer.request_absolute_cursor_position = _capped_request
        renderer.report_absolute_cursor_row = _capped_report

        # The completions dropdown: prompt_toolkit ships it as a floating menu whose
        # fit math expects reserved menu space inside the input region
        # (`reserve_space_for_menu`). This compact panel gives the input region a
        # single row (a taller reserve would permanently enlarge the box), so that
        # math computes a zero-height menu and the dropdown never paints. Replace the
        # default floats with one that parks directly ABOVE the input row — the box,
        # metadata row and footer stay visible (OpenCode-style) — and grows upward over
        # the transcript as tall as the screen allows.
        from prompt_toolkit.filters import has_completions, is_done
        from prompt_toolkit.layout.containers import Float, FloatContainer

        def _has_default_menu_float(c) -> bool:
            for f in getattr(c, "floats", []):
                chain = getattr(f, "content", None)
                for _ in range(4):
                    if chain is None:
                        break
                    if "CompletionsMenu" in type(chain).__name__:
                        return True
                    chain = getattr(chain, "content", None)
            return False

        class _AboveInput:
            """Stand-in for the float's `bottom`, read at draw time by
            `FloatContainer._draw_float`, which computes
            `ypos = (container height - float height) - bottom`. This object's
            `__rsub__` returns `-rows`, so the float's bottom row lands one row above
            the input row: an always-visible gap under the menu and room to grow
            upward over the transcript, whatever the completion menu's height."""

            def __init__(self, repl: "Repl"):
                self.repl = repl

            def __rsub__(self, other) -> int:
                return -self.repl._menu_row_count()

        stack = [self.session.layout.container]
        found = False
        while stack:
            c = stack.pop()
            if isinstance(c, FloatContainer) and _has_default_menu_float(c):
                c.floats = [
                    Float(left=0, bottom=_AboveInput(self), height=self._menu_row_count,
                          z_index=10 ** 8,
                          content=ConditionalContainer(
                              Window(
                                  FormattedTextControl(self._completion_menu_screen,
                                                       show_cursor=False),
                                  style="", always_hide_cursor=True,
                                  wrap_lines=False),
                              filter=has_completions & ~is_done)),
                ]
                found = True
                break
            for sub in (getattr(c, "content", None),
                        getattr(c, "alternative_content", None)):
                if sub is not None and hasattr(sub, "write_to_screen"):
                    stack.append(sub)
            stack.extend(getattr(c, "children", None) or [])
            stack.extend(getattr(f, "content", None)
                         for f in getattr(c, "floats", []))
        if not found:   # the default menu float shape changed upstream — surface it in e2e dumps
            try:
                import sys
                print("<MENU FLOATS NOT SWAPPED>", file=sys.stderr)
            except Exception:
                pass
        return self.session

    SEP = " <tb.sep>·</tb.sep> "
    # OpenCode-style prompt panel: a left accent bar spans the input and the metadata row,
    # sitting on a raised panel background, with a footer row below.
    BAR = "┃"
    BAR_IN = "<box.bar>┃</box.bar>    "   # accent bar + four cells of padding, in front of input lines

    def _geo(self, cols: Optional[int] = None) -> Tuple[int, int]:
        """(left margin, panel width): the panel column centered in the terminal, so the
        whole app (panel, info row, footer, transcript) sits in the middle like OpenCode."""
        if cols is None:
            try:
                from prompt_toolkit.application import get_app
                cols = get_app().output.get_size().columns
            except Exception:
                from shutil import get_terminal_size
                cols = get_terminal_size((80, 24)).columns
        width = max(40, min(cols, CONTENT_MAX))
        return max(0, (cols - width) // 2), width

    def _bar_continuation(self, width: int, line_number: int, wrap_count: int):
        """prompt continuation for wrapped/multiline input lines: keep the panel's accent
        bar in front of every input line, like OpenCode's prompt box."""
        left, _ = self._geo()
        return [("", " " * left), ("class:box.bar", self.BAR), ("", "    ")]

    PLACEHOLDERS = [
        "Ask anything…",
        "Fix a TODO in the codebase",
        "What is the tech stack of this project?",
        "Explain how this codebase is structured",
        "Fix the failing tests",
    ]

    def _placeholder_text(self):
        """Rotating prompt placeholder (OpenCode cycles suggestions too).

        The placeholder renders at the end of the (empty) input line, so it fills its own
        row with the panel background up to the panel's right edge."""
        if getattr(self, "is_busy", False):
            text = "Queue a message…"
        else:
            try:
                idx = int(time.time() // 4) % len(self.PLACEHOLDERS)
            except Exception:
                idx = 0
            text = self.PLACEHOLDERS[idx]
        _, width = self._geo()
        tail = " " * max(0, width - 3 - len(text))
        return HTML(f"<box.dim>{text}</box.dim><box.bg>{tail}</box.bg>")

    def _panel_fill(self):
        """Input-row fill: raises every cell of the typed text onto the panel background
        and fills from the accent bar to the panel's right edge.

        Runs as an input processor (before the placeholder appends), so it skips the row
        when the placeholder is visible — the placeholder fills its own row instead. A
        bg-only class keeps the text colors; the empty area left of the panel margin stays
        untouched (that is why the window itself is not painted)."""
        repl = self

        from prompt_toolkit.formatted_text.utils import fragment_list_to_text
        from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput
        from prompt_toolkit.utils import get_cwidth

        class _Fill(Processor):
            def apply_transformation(self, ti: TransformationInput) -> Transformation:
                session = getattr(repl, "session", None)
                if session is not None and session.default_buffer.text == "":
                    return Transformation(ti.fragments)   # placeholder owns this row
                _, width = repl._geo()
                out = [(style + " class:box.fill" if style.strip() else "class:box.fill", text)
                       for style, text in ti.fragments]
                tail = width - 3 - get_cwidth(fragment_list_to_text(ti.fragments))
                if tail > 0:
                    out.append(("class:box.fill", " " * tail))
                return Transformation(out)
        return _Fill()

    def _input_prefix(self) -> List[Tuple[str, str]]:
        """The accent bar + four cells of padding in front of the typed text."""
        left, _ = self._geo()
        return [("", " " * left), ("class:box.bar", self.BAR), ("", "    ")]

    MENU_MAX_ROWS = 12   # dropdown rows visible at once: a tall palette, not a sliver

    def _autoselect_first(self, buf) -> None:
        """Pre-highlight the first (closest) match whenever completions refresh."""
        st = getattr(buf, "complete_state", None)
        if st is not None and st.completions and st.complete_index is None:
            st.go_to_index(0)

    def _menu_row_count(self) -> int:
        """Rows the completion dropdown shows at once: everything the screen allows
        (capped at MENU_MAX_ROWS) instead of a fixed sliver of four."""
        state = getattr(getattr(getattr(self, "session", None), "default_buffer", None),
                        "complete_state", None)
        total = len(getattr(state, "completions", None) or [])
        if not total:
            return min(4, self.MENU_MAX_ROWS)
        try:
            from prompt_toolkit.application import get_app
            rows = get_app().output.get_size().rows
        except Exception:
            rows = 24
        # keep the input row and a little transcript context visible below the menu
        return max(1, min(total, self.MENU_MAX_ROWS, max(4, rows - 6)))

    def _completion_menu_screen(self):
        """The completions dropdown drawn INSIDE the panel (OpenCode-style): while the
        buffer has an active completion state, this overlay parks directly ABOVE the
        input row. Each row lists one completion with its description; the selected row
        carries the highlight, and the bottom row right-aligns a current/total counter.
        pt's stock float cannot render here — the compact panel leaves the input region
        one row tall, so its fit math collapses to zero, and a reserve
        (`reserve_space_for_menu`) would permanently enlarge the box."""
        session = getattr(self, "session", None)
        if session is None:
            return ""
        from prompt_toolkit.utils import get_cwidth

        state = session.default_buffer.complete_state
        comps = list(getattr(state, "completions", None) or [])
        if not comps:
            return ""
        total = len(comps)
        sel = getattr(state, "complete_index", None)
        sel = 0 if sel is None else max(0, min(sel, total - 1))
        n = self._menu_row_count()
        base = max(0, min(sel - 3, total - n))
        visible = comps[base:base + n]

        left, width = self._geo()
        inner = max(10, width - 3)          # bg columns, aligned with the input text
        labels = [(c.display_text or c.text or "").replace("\n", " ") for c in comps]
        label_w = min(max([get_cwidth(x) for x in labels] or [0]), 26)
        pos = f"{sel + 1}/{total}"
        pos_w = get_cwidth(pos)

        out: List[Tuple[str, str]] = []
        n_visible = len(visible)
        for i, comp in enumerate(visible):
            label = labels[base + i][:label_w]
            meta = (comp.display_meta_text or "").replace("\n", " ")
            cur = base + i == sel
            cls = ("class:completion-menu.completion.current" if cur
                   else "class:completion-menu")
            row = [("", " " * (left + 3)), (cls, f"{label:<{label_w}}")]
            used = label_w
            # description badge, clipped to whatever the counter leaves
            if i == n_visible - 1:
                room = inner - used - pos_w - 2
            else:
                room = inner - used - 1
            if meta and room > 2:
                meta_txt = meta[:room].rstrip()
                meta_cls = ("class:completion-menu.meta.completion.current" if cur
                            else "class:completion-menu.meta.completion")
                row.append((cls, " " * 2))
                row.append((meta_cls, meta_txt))
                used += 2 + get_cwidth(meta_txt)
            tail = inner - used - (pos_w if i == n_visible - 1 else 0)
            if tail > 0:
                row.append((cls, " " * tail))
            if i == n_visible - 1:
                row.append(("class:completion-menu.meta.completion", pos))
            out.extend(row)
            out.append(("", "\n"))
        del out[-1]
        return out

    def _toolbar(self):
        a = self.agent
        self._sync_scan_visuals()
        busy = getattr(self, "is_busy", False)
        left, width = self._geo()
        self._toolbar_height = 4  # panel pad row + metadata row + dark gap + footer, always

        # The metadata row lives INSIDE the prompt box, directly under the padded input
        # row: the same stats in every state — a [working…]/[N queued] flag while busy,
        # last-turn context when idle. The animated preloader hovers above the box
        # instead, so the box keeps one constant height (patch_stdout stays in step).
        from prompt_toolkit.formatted_text import FormattedText, to_formatted_text
        mode = a.permissions.mode
        ratio = a.context_ratio()
        gauge = _segmented_gauge(ratio, blocks=6)
        ctx_cls = "box.warn" if ratio >= 0.8 else "box.val"
        s = a.last_stats if busy and getattr(a.last_stats, "model_calls", 0) else self.turn_stats

        def short(n: int) -> str:
            return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e4 else f"{n:,}"

        meta = {
            "mode": f"<box.mode-{mode}>{escape_html(mode)}</box.mode-{mode}>",
            "model": f"<box.model>{escape_html(a.model)}</box.model>",
            "provider": f"<box.dim>{escape_html(a.provider_name)}</box.dim>",
            "context": f"<{ctx_cls}>context [{gauge}] {ratio:.0%}</{ctx_cls}>",
        }
        scans = [f"checking {escape_html(n)} {s_scan.progress()}" for n, s_scan in self.scanners.items() if s_scan.running]
        if scans:
            meta["scans"] = f"<box.dim>{escape_html(' · '.join(scans))}</box.dim>"
        if getattr(self, "message_queue", None):
            meta["working"] = f"<box.warn>[{len(self.message_queue)} queued]</box.warn>"
        elif busy:
            meta["working"] = "<box.dim>[working…]</box.dim>"
        if busy and s:
            speed = s.completion_tokens / s.duration if s.duration else 0.0
            meta["io"] = f"<box.in>↑ {short(s.prompt_tokens)}</box.in> <box.out>↓ {short(s.completion_tokens)}</box.out>"
            meta["speed"] = f"<box.speed>{speed:.1f}</box.speed> <box.dim>tok/s</box.dim>"
            if s.tool_calls:
                meta["tools"] = f"<box.val>{s.tool_calls}</box.val> <box.dim>tool call{'s' if s.tool_calls != 1 else ''}</box.dim>"
        if a.pinned:
            meta["pinned"] = f"<box.val>{len(a.pinned)}</box.val> <box.dim>pinned</box.dim>"
        if getattr(self, "multiline_mode", False):
            meta["multiline"] = "<box.val>multiline</box.val>"
        pending_images = getattr(self, "_pending_images", [])
        if pending_images:
            n = len(pending_images)
            meta["images"] = f"<box.warn>[{n} image{'s' if n != 1 else ''}]</box.warn>"

        # One metadata row, dropping the least important info rather than wrapping.
        core = ["mode", "model", "provider", "context"]
        optional = ["images", "scans", "working", "io", "speed", "tools", "pinned", "multiline"]
        row = self._meta_row(meta, core, optional, max(10, width - 3))
        tail = " " * max(0, width - 3 - self._visible_len(row))
        row1 = list(to_formatted_text(HTML((" " * left) + self.BAR_IN + row
                                           + (f"<box.bg>{tail}</box.bg>" if tail else ""))))

        # A blank panel row directly under the input line: the box breathes (OpenCode).
        pad_row = list(to_formatted_text(
            HTML((" " * left) + f"<box.bar>{self.BAR}</box.bar><box.bg>{' ' * (width - 1)}</box.bg>")))

        # One bigger box: pad row under the input, then the metadata row, then a dark gap
        # and the dim footer (OpenCode layout).
        return FormattedText(list(pad_row) + [("", "\n")] + list(row1) + [("", "\n\n")]
                             + list(to_formatted_text(HTML(self._footer_row(width, busy)))))

    def _shimmer_frags(self, width: int) -> List[Tuple[str, str]]:
        """The busy preloader as formatted-text frags — a shimmer wave sweeping over the icon
        and a rotating pondering verb, plus turn stats. `_thinking_screen` renders this line
        ABOVE the prompt box while a turn runs."""
        from hubble.board import CLAUDE_GLYPHS, _fmt_tokens
        from hubble import spinner

        events = getattr(self.agent, "events", None)
        frame = int(time.time() * 10)

        active = getattr(spinner, "_ACTIVE_SHIMMER", None)
        active_label = getattr(getattr(active, "shimmer", None), "label", None) if active and not getattr(active, "_stopped", False) else None
        current_tool = getattr(events, "current_tool", None) or getattr(events, "current_activity", None)

        # Multiple icon sets for dynamic morphing across pondering stages (Claude Code & Command Code style)
        CLAUDE_ICON_SETS = [
            ["✻", "✼", "✽", "✾", "✿", "❀", "❁", "❂"],             # Classic Claude blooming flower
            ["✦", "✧", "✶", "✷", "✸", "✹", "✺", "✢"],             # Command Code starbursts & sparks
            ["✿", "❀", "❁", "❂", "❁", "❀", "✿", "✾"],             # Botanical breathing bloom
            ["✶", "✻", "✦", "❂", "✺", "✹", "✷", "✧"],             # Radiant star & sunburst
            ["⌾", "◎", "◇", "◈", "◆", "✦", "✧", "❂"],             # Cosmic prisms & lens
            ["✢", "✳", "∗", "✻", "✼", "✽", "✦", "✧"],             # Star cluster
        ]

        CLAUDE_VERBS = [
            "Pondering", "Synthesizing", "Cogitating", "Contemplating", "Analyzing",
            "Focusing the lens", "Connecting dots", "Percolating", "Aligning mirrors",
            "Marinating", "Noodling", "Formulating response", "Caramelizing",
            "Decoding signals", "Calculating trajectories", "Consulting the stars",
            "Reviewing", "Evaluating", "Reflecting", "Drifting through code", "Thinking",
        ]
        sh_obj = getattr(active, "shimmer", None)
        t_start = getattr(events, "turn_start_time", 0.0) or (getattr(sh_obj, "start", 0.0) if sh_obj else 0.0)
        elapsed = max(0.0, time.time() - t_start) if t_start else 0.0

        if active_label:
            words = active_label + "…"
            glyph = CLAUDE_GLYPHS[frame % len(CLAUDE_GLYPHS)]
        elif current_tool and current_tool not in ("Thinking...", "Generating response...", "Processing results..."):
            words = current_tool + "…"
            glyph = CLAUDE_GLYPHS[frame % len(CLAUDE_GLYPHS)]
        else:
            verb_idx = int(elapsed / 2.8) % len(CLAUDE_VERBS)
            words = CLAUDE_VERBS[verb_idx] + "…"
            active_set = CLAUDE_ICON_SETS[verb_idx % len(CLAUDE_ICON_SETS)]
            glyph = active_set[frame % len(active_set)]

        tools = getattr(events, "tool_calls_this_turn", 0)
        s = getattr(getattr(self, "agent", None), "last_stats", None)

        # Shimmer wave over the icon and letters (Claude Code & Command Code style)
        sweep = len(words) + 8
        pos = (elapsed * 14) % sweep - 4
        icon_d = abs(-1 - pos)
        icon_style = "fg:#ffffff bold" if icon_d < 1.4 else "fg:#ffe08a bold" if icon_d < 2.8 else "fg:#ffd75f bold"
        frags = [(icon_style, f"{glyph} ")]
        for i, ch in enumerate(words):
            d = abs(i - pos)
            style = "fg:#ffffff bold" if d < 1.2 else "fg:#ffe08a bold" if d < 2.5 else "fg:#d7b055"
            frags.append((style, ch))

        stats_parts = [f"{int(elapsed)}s"]
        if s and (s.prompt_tokens or s.completion_tokens):
            stats_parts.append(f"{_fmt_tokens(s.prompt_tokens + s.completion_tokens)} tok")
        elif getattr(events, "reasoning_chars", 0):
            r_tok = int(events.reasoning_chars / 3.8)
            if r_tok:
                stats_parts.append(f"{_fmt_tokens(r_tok)} tok")
        if tools:
            stats_parts.append(f"{tools} tool{'s' if tools != 1 else ''}")
        stats_parts.append("ctrl+c to interrupt")

        frags.append(("fg:#8a8a8a", f"  ({' · '.join(stats_parts)})"))
        pending_images = getattr(self, "_pending_images", [])
        if pending_images:
            n = len(pending_images)
            frags.append(("fg:#ffaf00 bold", f"  [{n} image{'s' if n != 1 else ''}]"))
        return frags

    def _shimmer_row(self, width: int) -> List[Tuple[str, str]]:
        """The preloader as an in-panel row (bar + padding + frags + bg tail). The live
        animation hovers above the box instead; this slot keeps the shimmer usable
        programmatically (tests, headless fallbacks)."""
        from prompt_toolkit.utils import get_cwidth

        frags = self._shimmer_frags(width)
        # The preloader lives INSIDE the panel: bar + padding + shimmer + bg tail.
        left, _ = self._geo()
        row = [("", " " * left), ("class:box.bar", self.BAR), ("class:box.bg", "  ")] + frags
        tail = " " * max(0, width - 3 - get_cwidth("".join(text for _, text in frags)))
        row.append(("class:box.bg", tail))
        return row

    def _thinking_screen(self) -> List[Tuple[str, str]]:
        """The animated preloader line rendered ABOVE the prompt box while a turn runs
        (Claude Code layout): the committed prompt scrolled past above it, this line
        animates below it, and the input panel sits at the bottom. Content aligns with
        the panel's text column; the row is one line tall, long text is simply clipped."""
        try:
            left, width = self._geo()
            frags = self._shimmer_frags(width)
        except Exception:
            return [("", "  Working…")]
        return [("", " " * (left + 3))] + frags

    @staticmethod
    def _visible_len(markup: str) -> int:
        return len(re.sub(r"<[^>]+>", "", markup).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))

    def _meta_row(self, meta: Dict[str, str], core: List[str], optional: List[str], width: int) -> str:
        def join(keys: List[str]) -> str:
            return self.SEP.join(meta[k] for k in keys if k in meta)
        keys = list(core) + [k for k in optional if k in meta]
        while len(keys) > len(core) and self._visible_len(join(keys)) > width:
            for k in reversed(optional):
                if k in keys:
                    keys.remove(k)
                    break
            else:
                break
        return join(keys)

    def _footer_row(self, width: int, busy: bool) -> str:
        """cwd on the left, keybind hints on the right (OpenCode footer), inside the content column."""
        left, _ = self._geo()
        hints = "<ft.hint>ctrl+c interrupt</ft.hint>" if busy else \
            "<ft.hint>shift+tab mode</ft.hint>  <ft.hint>/ commands</ft.hint>"
        hint_w = self._visible_len(hints)
        cwd = escape_html(str(self.agent.ctx.root or "").replace(str(Path.home()), "~"))
        budget = max(4, width - 1 - hint_w)   # keep room for the gap before the hints
        if len(cwd) > budget:
            cwd = "…" + cwd[-(budget - 1):]
        pad = " " * max(1, width - len(cwd) - hint_w)
        return (" " * left) + f"<ft.dir>{cwd}</ft.dir>{pad}{hints}"

    def _run_footer(self):
        """One dim status line pinned under the live display while the agent works."""
        from rich.text import Text
        a = self.agent
        s = a.last_stats
        mode = a.permissions.mode
        mode_style = {"accept-edits": "bold #5fd787", "plan": "bold #ffd75f", "yolo": "bold #ff5f5f"}.get(mode, "#b8b8b8")
        ratio = a.context_ratio()
        gauge = _segmented_gauge(ratio, blocks=6)
        session_total = a.total_prompt_tokens + a.total_completion_tokens
        speed = s.completion_tokens / s.duration if s.duration else 0.0

        def short(n: int) -> str:
            return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e4 else f"{n:,}"

        t = Text("  ")
        if len(self.providers) > 1:
            t.append("● ", style="bold #ffd75f")
            t.append(f"{a.provider_name} ", style="#8a8a8a")
            t.append(a.model, style="bold #ffd75f")
        else:
            t.append(f"● {a.model}", style="bold #ffd75f")
        sep = ("  ·  ", "#444444")
        t.append(*sep).append(f"[{mode}]", style=mode_style)
        t.append(*sep).append(f"context [{gauge}] {ratio:.0%}", style="bold #ffaf00" if ratio >= 0.8 else "#b8b8b8")
        t.append(*sep).append(f"↑ {short(s.prompt_tokens)}", style="#ffd75f").append(" ", style="")
        t.append(f"↓ {short(s.completion_tokens)}", style="#87d7af").append(" this turn", style="#8a8a8a")
        t.append(*sep).append("session ", style="#8a8a8a").append(
            short(session_total), style="#b8b8b8")
        t.append(*sep).append(f"{speed:.1f}", style="#5fd7af").append(" tok/s", style="#8a8a8a")
        if s.tool_calls:
            t.append(*sep).append(str(s.tool_calls), style="#b8b8b8").append(
                f" tool call{'s' if s.tool_calls != 1 else ''}", style="#8a8a8a")
        if getattr(self, "message_queue", None):
            t.append(*sep).append(f"[{len(self.message_queue)} queued]", style="bold #ffaf00")
        elif getattr(self, "is_busy", False):
            t.append(*sep).append("[working...]", style="#8a8a8a")
        return t

    @classmethod
    def _pack(cls, parts: List[str], width: int) -> List[str]:
        def visible(markup: str) -> int:
            text = re.sub(r"<[^>]+>", "", markup)
            return len(text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))

        sep_w = visible(cls.SEP)
        lines: List[List[str]] = [[]]
        used = 0
        for part in parts:
            w = visible(part)
            if lines[-1] and used + sep_w + w > width:
                lines.append([])
                used = 0
            used += (sep_w if lines[-1] else 0) + w
            lines[-1].append(part)
        return [cls.SEP.join(line) for line in lines]

    from hubble.theme import get_prompt_style
    TOOLBAR_STYLE = get_prompt_style("terminal")

    def _sync_scan_visuals(self):
        """Called on every prompt redraw (at least once a second): keep the home screen's model
        line in step with the running scan, and report a finished scan right away instead of
        waiting for the next Enter."""
        # Only when there really is something to print, and only schedule it once: run_in_terminal
        # redraws the prompt, which calls this again -- anything looser here loops forever (flicker).
        if self.updates.has_notice() and not self._update_scheduled:
            self._update_scheduled = True
            try:
                from prompt_toolkit.application import get_app, run_in_terminal
                get_app().loop.call_soon(lambda: run_in_terminal(self._announce_update))
            except Exception:
                self._update_scheduled = False  # not inside a prompt; the main loop shows it
        key = tuple((n, s.status, s.done, s.retry_done) for n, s in self.scanners.items())
        if key == self._scan_key:
            return
        finished = self._scan_key is not None and any(
            n in self._unannounced and not s.running for n, s in self.scanners.items())
        self._scan_key = key
        if self._home is not None:
            info = self._home_info_cache if not finished and self._home_info_cache else self._home_info()
            self._home_info_cache = info
            cur = self.scanners.get(self.agent.provider_name)
            note = cur.summary() if cur and cur.running else ""
            from hubble.banner import home_info_lines
            self._home = home_info_lines(**info, scan_note=note)
        if finished:
            try:
                from prompt_toolkit.application import get_app, run_in_terminal
                get_app().loop.call_soon(lambda: run_in_terminal(self._announce_scans))
            except Exception:
                pass  # not inside a prompt; the main loop announces before the next one

    def _stats_line(self) -> str:
        a = self.agent
        s = self.turn_stats
        session_total = a.total_prompt_tokens + a.total_completion_tokens
        if s is None or not s.model_calls:
            return f"<tb.dim>no turns yet</tb.dim>{self.SEP}<tb.dim>session</tb.dim> <tb.val>{session_total:,}</tb.val>"
        speed = s.completion_tokens / s.duration if s.duration else 0.0
        parts = [f"<tb.in>↑ {s.prompt_tokens:,}</tb.in> <tb.dim>in</tb.dim>  "
                 f"<tb.out>↓ {s.completion_tokens:,}</tb.out> <tb.dim>out</tb.dim>",
                 f"<tb.val>{s.duration:.1f}s</tb.val>",
                 f"<tb.speed>{speed:.1f}</tb.speed> <tb.dim>tok/s</tb.dim>"]
        if s.tool_calls:
            parts.append(f"<tb.val>{s.tool_calls}</tb.val> <tb.dim>tool call{'s' if s.tool_calls != 1 else ''}</tb.dim>")
        parts.append(f"<tb.dim>session</tb.dim> <tb.val>{session_total:,}</tb.val>")
        return self.SEP.join(parts)

    def _home_info(self):
        a = self.agent
        known = [m for m in provider_models(a.provider_name) if m.get("available") is not False]
        update_banner = self.updates.get_banner_text() if hasattr(self, "updates") else None
        return dict(version=__version__, provider=a.provider_name, model=a.model, mode=a.permissions.mode,
                    root=str(a.ctx.root), session_id=a.session.id if a.session else None,
                    memory_files=[p.name for p, _ in a.memory], model_count=len(known) or None,
                    provider_count=len(self.providers), resumable=max(0, len(self.store.list()) - 1),
                    show_provider=len(self.providers) > 1, update_banner=update_banner)

    def _live_home_ok(self) -> bool:
        return (bool(self.agent.settings.get("home_animation", True)) and sys.stdin.isatty()
                and sys.stdout.isatty() and console.is_terminal and not console.legacy_windows)

    def banner(self):
        """Show the home screen: animated inside the next prompt when possible, else printed once."""
        if self._live_home_ok():
            from hubble.banner import home_info_lines
            self._home = home_info_lines(**self._home_info())
            self._home_t0 = time.time()
        else:
            from hubble.banner import canvas_bg, render_home
            render_home(console, logo_style=self.agent.settings.get("logo"), bg=canvas_bg(), **self._home_info())

    def _home_message(self):
        """Prompt message for the first prompt: the whole home screen, re-rendered every frame."""
        from prompt_toolkit.application import get_app
        from prompt_toolkit.formatted_text import FormattedText
        from hubble.banner import canvas_bg, compose_home_fragments
        try:
            app = get_app()
            size = app.output.get_size()
            typed = app.current_buffer.text
        except Exception:
            return HTML("<box.bar>┃</box.bar>  ")
        # Leave room for the prompt panel (spacer + input + pad + metadata rows and the
        # footer), plus the completion menu while typing / or @.
        toolbar = getattr(self, "_toolbar_height", 4)
        budget = size.rows - 4 - toolbar - (10 if typed[:1] in ("/", "@") or " @" in typed else 0)
        cached = getattr(self, "_home_frame", None)
        # When user is typing, freeze animation frame to eliminate input latency and prevent keystroke lag.
        if typed.strip() and cached is not None and cached[2] == budget and cached[3] == size.columns:
            return cached[1]
        elapsed = time.time() - self._home_t0
        fps_step = int(elapsed * 60)
        if cached and cached[0] == fps_step and cached[2] == budget and cached[3] == size.columns:
            return cached[1]
        status, tips = self._home
        left, width = self._geo(size.columns)
        bg = canvas_bg()
        frags = compose_home_fragments(width, max(budget, 3), elapsed, status, tips, __version__,
                                       logo_style=self.agent.settings.get("logo"), bg=bg,
                                       pad=max(0, size.columns - left - width) if bg else 0)
        prefix = self._input_prefix()
        if bg:
            prefix[0] = (f"bg:{bg}", prefix[0][1])   # the input row's margin is canvas too
        frame = FormattedText(_indent_frags(frags, left, style=f"bg:{bg}" if bg else "")
                              + [("", "\n")] + prefix)
        self._home_frame = (fps_step, frame, budget, size.columns)
        return frame

    def _prompt_message(self):
        """Prompt message: the accent-bar prefix of the input row — one row, in every state.

        The busy preloader hovers above the box (its own layout row), so the prompt
        region's height only changes with the board/the thinking row appearing or
        hiding, and patch_stdout repaints stay in step."""
        from prompt_toolkit.formatted_text import FormattedText

        events = getattr(getattr(self, "agent", None), "events", None)
        board = getattr(events, "board", None)
        if board is not None and hasattr(board, "to_formatted_text"):
            frags = board.to_formatted_text()
            if frags:
                left, _ = self._geo()
                return FormattedText(_indent_frags(frags, left) + [("", "\n")] + self._input_prefix())
        return FormattedText(self._input_prefix())

    def fallback_for(self, provider: str, model: str):
        choice = resolve_fallback(self.agent.settings, self.providers, provider, model)
        if not choice or choice[0] not in self.providers and choice[0] != self.agent.provider_name:
            return None
        return self.client(choice[0]), choice[1]

    def cmd_fallback(self, arg):
        prov = self.agent.provider_name
        per = dict(self.agent.settings.get("fallback_models") or {})
        current = per.get(prov) or "auto"
        if arg:
            choice = arg.strip()
        elif sys.stdin.isatty():
            auto = resolve_fallback({**self.agent.settings, "fallback_models": {}}, self.providers, prov, self.agent.model)
            auto_meta = f"now: {auto[0]}: {auto[1]}" if auto else "nothing suitable found"
            items = [("auto", "Automatic", auto_meta), ("off", "Off", "never switch models on errors")]
            items += [(v[1], label, meta) for v, label, meta in self.model_items(only=prov) if v[1] != self.agent.model]
            choice = pick(f"Fallback model for {prov}", items, current=current)
            if not choice:
                return
        else:
            console.print(f"Fallback for {escape(prov)}: {escape(current)}")
            return
        if choice == "auto":
            per.pop(prov, None)
        else:
            per[prov] = choice
        self.agent.settings["fallback_models"] = per
        save_user_setting("fallback_models", per)
        console.print(f"[green]Fallback for {escape(prov)}: {escape(choice)}[/green]")

    def client(self, name: str) -> OpenAICompatProvider:
        if name not in self.clients:
            from hubble.providers import make_client
            self.clients[name] = make_client(self.providers[name])
        return self.clients[name]

    def start_scan(self, name: str, reason: str, quiet: bool = False) -> bool:
        cfg = self.providers.get(name)
        if not cfg:
            return False
        scanner = self.scanners.get(name)
        if scanner is None:
            scanner = self.scanners[name] = ModelScanner(cfg.base_url, cfg.api_key, output=scan_file(name),
                                                         kind=getattr(cfg, "kind", "openai"))
        if not scanner.start():
            return False
        self._unannounced.add(name)
        if quiet:
            self._quiet_scans.add(name)
        else:
            self._quiet_scans.discard(name)
            console.print(f"[dim]  {reason} Checking which {escape(name)} models are available in the background "
                          "(progress in the bottom bar)...[/dim]")
        return True

    def _announce_update(self):
        notice = self.updates.pending_notice()
        if notice:
            hint = self.updates.get_banner_text()
            if hint:
                console.print(f"[bold #ffd75f]{escape(hint)}[/bold #ffd75f]")
            else:
                console.print(f"[bold yellow]⬆ {escape(notice)}[/bold yellow]")

    def _announce_scans(self):
        for name in list(self._unannounced):
            scanner = self.scanners.get(name)
            if not scanner or scanner.running:
                continue
            self._unannounced.discard(name)
            quiet = name in self._quiet_scans
            self._quiet_scans.discard(name)
            if scanner.status == "done" and not quiet:
                console.print(f"[green]{escape(name)}: {scanner.working} of {scanner.total} models are "
                              "available.[/green] [dim]/model to see them[/dim]")
            elif scanner.status == "failed":
                console.print(f"[yellow]{escape(name)}: model check failed ({escape(scanner.error)}); "
                              "keeping the previous list.[/yellow]")

    def _refresh_stale_scans(self):
        raw = self.agent.settings.get("model_refresh_hours", 0)
        if raw is None:  # explicitly disabled ("model_refresh_hours": null)
            return
        hours = float(raw)  # 0 (the default): check every startup; >0: only once that stale
        for name, cfg in self.providers.items():
            # Claude's check is just its free model listing, so it always runs.
            if not cfg.check_models and getattr(cfg, "kind", "openai") != "anthropic":
                continue
            age = scan_age_hours(scan_file(name))
            if age is None or hours <= 0 or age >= hours:
                # Silent: the bottom bar already shows progress, and only a failure is worth a line.
                self.start_scan(name, "", quiet=True)

    def _start_worker(self):
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._shutdown_event.clear()
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()

    def _worker_loop(self):
        while not self._shutdown_event.is_set():
            try:
                task = self._task_queue.get(timeout=0.2)
            except Exception:
                continue
            if task is None:
                break
            text = task
            self.is_busy = True
            if getattr(self, "session", None) is not None:
                self.session.refresh_interval = 0.1
                try:
                    self.session.app.invalidate()
                except Exception:
                    pass
            try:
                self.send(text)
            except Exception as e:
                console.print(f"[red]Error: {escape(str(e))}[/red]")
            finally:
                while self.message_queue and not self._shutdown_event.is_set():
                    next_text = self.message_queue.pop(0)
                    from hubble.theme import get_palette
                    from hubble.ui import ACTIVE_THEME
                    pal = get_palette(ACTIVE_THEME)
                    p_col = pal.user_prefix or "bold #ffd75f"
                    u_col = pal.user_text or "#ffffff"
                    console.print(f"[{p_col}]❯[/{p_col}] [{u_col}]{escape(next_text)}[/{u_col}]")
                    try:
                        self.send(next_text)
                    except Exception as e:
                        console.print(f"[red]Error: {escape(str(e))}[/red]")
                self.is_busy = False
                if getattr(self, "session", None) is not None:
                    self.session.refresh_interval = 1.0
                    try:
                        self.session.app.invalidate()
                    except Exception:
                        pass

    def cmd_queue(self, arg: str = ""):
        if self.message_queue:
            console.print(f"[yellow]Queued prompts ({len(self.message_queue)}):[/yellow]")
            for i, qm in enumerate(self.message_queue, 1):
                console.print(f"  {i}. {escape(qm)}")
        else:
            console.print("[dim]No queued prompts.[/dim]")

    def cmd_clear_queue(self, arg: str = ""):
        count = self._clear_queue()
        console.print(f"[yellow]Cleared {count} queued prompt(s).[/yellow]")

    def _clear_queue(self) -> int:
        """Drop queued prompts, deleting the temp files of images pasted with them."""
        count = len(self.message_queue)
        for prompt in self.message_queue:
            for path in getattr(prompt, "images", []):
                path.unlink(missing_ok=True)
        self.message_queue.clear()
        return count

    def cmd_update(self, arg: str = ""):
        """Check for updates and upgrade Hubble."""
        from hubble.update import UpdateChecker, upgrade_command, is_newer, _fetch_latest
        checker = UpdateChecker(enabled=True)
        # Force a fresh check
        latest = _fetch_latest()
        if latest is None:
            console.print("[yellow]Could not check for updates (network error or PyPI unavailable).[/yellow]")
            return
        if not is_newer(latest):
            console.print(f"[green]You are on the latest version ({__version__}).[/green]")
            return
        console.print(f"[bold cyan]Update available:[/bold cyan] hubble {__version__} → {latest}")
        console.print(f"Run: [bold]{upgrade_command()}[/bold]")
        if arg.strip().lower() in ("yes", "y", "auto"):
            import subprocess
            try:
                console.print("[dim]Upgrading...[/dim]")
                result = subprocess.run(upgrade_command(), shell=True, check=True)
                console.print("[green]Upgrade complete. Restart Hubble to use the new version.[/green]")
            except subprocess.CalledProcessError as e:
                console.print(f"[red]Upgrade failed: {e}[/red]")

    def _ask_approval_bridge(self, tool, args: Dict[str, Any], preview: Optional[str]) -> Tuple[str, str]:
        session = getattr(self, "session", None)
        app = getattr(session, "app", None) if session else None
        if app is not None and getattr(app, "is_running", False) and getattr(app, "loop", None):
            resp_queue = queue.Queue()
            saved_draft = ""
            try:
                buf = getattr(app, "current_buffer", None)
                if buf:
                    saved_draft = buf.text
            except Exception:
                pass
            self._pending_approval = (tool, args, preview, resp_queue, saved_draft)
            try:
                app.loop.call_soon_threadsafe(lambda: app.exit(result=self._APPROVAL_SENTINEL))
            except Exception:
                return self.agent.events._ask(tool, args, preview)
            return resp_queue.get()
        return self.agent.events._ask(tool, args, preview)

    def run(self, initial_prompt: Optional[str] = None):
        from hubble import spinner
        from prompt_toolkit.patch_stdout import patch_stdout
        spinner.FOOTER = None  # Single bottom toolbar owned by prompt_toolkit (no duplicate middle bar)
        self._home = None
        self.agent.start_session("resume" if self.agent.messages else "startup")
        self.agent.events.approval_bridge = self._ask_approval_bridge
        self.banner()
        self._refresh_stale_scans()
        self.updates.start()
        self._start_worker()
        session = self._prompt_session()
        if initial_prompt:
            self._home = None
            self.handle(initial_prompt)
        pending_default = ""
        with patch_stdout(raw=True):
            while not self._shutdown_event.is_set():
                try:
                    self._announce_scans()
                    self._announce_update()
                    if self._home is not None:
                        # Animated home screen lives in the prompt until the first message is sent;
                        # the last frame stays in the scrollback.
                        text = session.prompt(self._home_message, refresh_interval=1 / 60)
                        self._home = None
                        session.refresh_interval = 1.0
                    else:
                        text = session.prompt(self._prompt_message, default=pending_default)
                        pending_default = ""
                except KeyboardInterrupt:
                    if self.is_busy:
                        if self.message_queue:
                            count = self._clear_queue()
                            console.print(f"[yellow]Cleared {count} queued message(s).[/yellow]")
                        self.agent.interrupt()
                        continue
                    now = time.time()
                    if now - self._last_ctrl_c < 2.0:
                        break
                    self._last_ctrl_c = now
                    console.print("[dim]Press Ctrl+C again to exit (or type /exit).[/dim]")
                    continue
                except EOFError:
                    break
                except Exception:
                    time.sleep(0.05)
                    try:
                        session = self._prompt_session()
                    except Exception:
                        pass
                    continue

                if text is self._APPROVAL_SENTINEL:
                    pending = getattr(self, "_pending_approval", None)
                    self._pending_approval = None
                    if pending:
                        tool, args, preview, resp_queue, saved_draft = pending
                        pending_default = saved_draft
                        try:
                            ans = self.agent.events._ask(tool, args, preview)
                        except (KeyboardInterrupt, EOFError):
                            ans = ("no", "Interrupted by user")
                            if self.is_busy:
                                self.agent.interrupt()
                        except Exception as e:
                            ans = ("no", str(e))
                        resp_queue.put(ans)
                    continue

                try:
                    if self.handle(text) is False:
                        break
                except KeyboardInterrupt:
                    if self.is_busy:
                        self.agent.interrupt()
                    else:
                        console.print("[yellow]Interrupted.[/yellow]")
        self._shutdown_event.set()
        for path in self._take_pending_images():   # pasted but never sent
            path.unlink(missing_ok=True)
        self._clear_queue()
        if self._worker_thread and self._worker_thread.is_alive():
            self._task_queue.put(None)
            self._worker_thread.join(timeout=1.0)
        spinner.FOOTER = None
        self.agent.shutdown()
        console.print("[dim]Bye.[/dim]")

    def handle(self, text: str):
        text = text.strip()
        if not text:
            return None
        if text.startswith("!"):
            self.run_shell(text[1:].strip())
            return None
        if text.startswith("#") and not text.startswith("##"):
            self.remember(text[1:].strip())
            return None
        if text.startswith("/") and not text.startswith("//"):
            name, _, arg = text[1:].partition(" ")
            cmd = self.command_names().get(name.lower())
            if not cmd:
                console.print(f"[yellow]Unknown command /{escape(name)}. Try /help.[/yellow]")
                return None
            return cmd.fn(arg.strip())
        if self._worker_thread and self._worker_thread.is_alive():
            if self.is_busy:
                self.message_queue.append(_QueuedPrompt(text, self._take_pending_images()))
                console.print(f"  [yellow]●[/yellow] [dim]Queued ({len(self.message_queue)} in queue):[/dim] [bold]{escape(text)}[/bold]")
                return None
            self._task_queue.put(_QueuedPrompt(text, self._take_pending_images()))
            time.sleep(0.04)
            return None
        self.send(text)
        return None

    def send(self, text: str):
        images: List[str] = []
        # A queued prompt carries the images pasted with it; [] means "none" (do not steal
        # images pasted for a later message). A plain str falls back to the pending images.
        pasted = getattr(text, "images", None)
        prompt = self.expand_mentions(text, images)
        prompt = self._attach_pasted_images(prompt, images, pasted)
        self.agent.run(prompt, images or None)
        if self.agent.last_stats.model_calls:
            self.turn_stats = self.agent.last_stats  # shown in the bar under the input box

    def paste_image(self) -> Optional[str]:
        """Grab an image from the clipboard; returns its [Image #N] placeholder, or None."""
        path = grab_clipboard_image()
        if path is None:
            return None
        self._pending_images.append(path)
        return f"[Image #{len(self._pending_images)}]"

    def _take_pending_images(self) -> List[Path]:
        """Detach the images pasted so far, so a queued prompt owns them: the next paste
        starts again at [Image #1], and two messages cannot consume each other's images."""
        pending, self._pending_images = self._pending_images, []
        return pending

    def _attach_pasted_images(self, prompt: str, images: List[str],
                              pasted: Optional[List[Path]] = None) -> str:
        if pasted is None:
            pasted = self._take_pending_images()
        for i, path in enumerate(pasted, 1):
            if f"[Image #{i}]" not in prompt:
                continue  # the placeholder was deleted before sending: drop that image
            try:
                images.append(data_url(path))
                console.print(f"[dim]  attached pasted image #{i}[/dim]")
            except (ImageError, OSError) as e:
                console.print(f"[yellow]Skipped image #{i}: {escape(str(e))}[/yellow]")
        for path in pasted:
            path.unlink(missing_ok=True)
        return prompt

    def expand_mentions(self, text: str, images: Optional[List[str]] = None) -> str:
        attachments = []
        for m in MENTION_RX.finditer(text):
            raw = m.group(1).rstrip(".,:;")
            r_lower = raw.lower()

            # 1. Semantic pseudo-mentions
            if r_lower == "git":
                try:
                    res = subprocess.run(["git", "status", "--short", "--branch"], cwd=self.agent.ctx.root, capture_output=True, text=True, timeout=5)
                    out = res.stdout.strip() or "(clean working tree)"
                    attachments.append(f'<git-status>\n{out}\n</git-status>')
                    console.print("[dim]  attached @git status[/dim]")
                except Exception as e:
                    console.print(f"[yellow]Failed to attach @git: {escape(str(e))}[/yellow]")
                continue
            if r_lower == "diff":
                try:
                    res = subprocess.run(["git", "diff", "HEAD"], cwd=self.agent.ctx.root, capture_output=True, text=True, timeout=5)
                    out = res.stdout.strip() or "(no uncommitted diff)"
                    attachments.append(f'<git-diff>\n{truncate(out, MAX_MENTION_CHARS)}\n</git-diff>')
                    console.print(f"[dim]  attached @diff ({len(out.splitlines())} lines)[/dim]")
                except Exception as e:
                    console.print(f"[yellow]Failed to attach @diff: {escape(str(e))}[/yellow]")
                continue
            if r_lower == "todos":
                if self.agent.ctx.todos:
                    todos_str = "\n".join(f"- [{'x' if t.get('done') else ' '}] {t.get('task') or t.get('text', '')}" for t in self.agent.ctx.todos)
                else:
                    todos_str = "(no active todos)"
                attachments.append(f'<todos>\n{todos_str}\n</todos>')
                console.print(f"[dim]  attached @todos ({len(self.agent.ctx.todos)} items)[/dim]")
                continue
            if r_lower in ("problems", "diagnostics", "errors"):
                problems_str = getattr(self.agent, "last_error", None) or "(no active errors or problems recorded)"
                attachments.append(f'<problems>\n{problems_str}\n</problems>')
                console.print("[dim]  attached @problems[/dim]")
                continue

            # 2. File and directory resolution (with fuzzy fallback)
            try:
                p = self.agent.ctx.resolve(raw)
            except Exception:
                p = None
            if p is None or not p.exists():
                candidates = []
                for entry in self.agent.ctx.root.rglob("*"):
                    if any(part in (".git", "__pycache__", "node_modules", ".venv", "venv") for part in entry.parts):
                        continue
                    if entry.is_file():
                        rel_str = str(entry.relative_to(self.agent.ctx.root)).replace("\\", "/")
                        if _is_subsequence(raw, rel_str):
                            candidates.append(entry)
                if len(candidates) == 1:
                    p = candidates[0]
                elif p is None or not p.exists():
                    continue

            if is_secret_path(p) and not self.agent.ctx.allow_secrets:
                console.print(f"[yellow]Skipped @{escape(raw)}: looks like a secrets file.[/yellow]")
                continue
            if p.is_file() and is_image(p) and images is not None:
                try:
                    images.append(data_url(p))
                    console.print(f"[dim]  attached image {escape(self.agent.ctx.rel(p))}[/dim]")
                except (ImageError, OSError) as e:
                    console.print(f"[yellow]Skipped @{escape(raw)}: {escape(str(e))}[/yellow]")
                continue
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                self.agent.ctx.read_mtimes[str(p)] = p.stat().st_mtime
                attachments.append(f'<file path="{self.agent.ctx.rel(p)}">\n{truncate(content, MAX_MENTION_CHARS)}\n</file>')
                console.print(f"[dim]  attached {escape(self.agent.ctx.rel(p))} ({len(content.splitlines())} lines)[/dim]")
            elif p.is_dir():
                listing = "\n".join(sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir())[:200])
                attachments.append(f'<directory path="{self.agent.ctx.rel(p)}">\n{listing}\n</directory>')
        if not attachments:
            return text
        return text + "\n\n" + "\n\n".join(attachments)

    def run_shell(self, cmd: str):
        if not cmd:
            return
        argv = list(self.agent.ctx.shell_argv) + [cmd]
        try:
            subprocess.run(argv, cwd=self.agent.ctx.root)
        except (OSError, KeyboardInterrupt) as e:
            console.print(f"[red]{escape(str(e))}[/red]")

    def remember(self, note: str):
        if not note:
            return
        path = self.agent.ctx.root / "HUBBLE.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else "# Project notes\n"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(existing.rstrip("\n") + f"\n- {note}\n")
        self.agent.reload_memory()
        console.print(f"[green]Saved to {escape(str(path))}[/green]")

    # ----- commands ------------------------------------------------------

    def cmd_help(self, arg):
        table = Table(show_header=False, box=None, padding=(0, 2))
        for cmd in self.commands.values():
            alias = f" [dim](/{', /'.join(cmd.aliases)})[/dim]" if cmd.aliases else ""
            table.add_row(f"[bold]/{cmd.name}[/bold] [dim]{escape(cmd.args)}[/dim]", escape(cmd.help) + alias)
        console.print(table)
        console.print("\n[bold]Input[/bold]\n"
                      "  @path          attach a file, directory, or fuzzy path (e.g. @test_job)\n"
                      "  @git / @diff   attach current git status or uncommitted diff\n"
                      "  @todos         attach active project task list\n"
                      "  Shift+Enter    insert a newline (also Esc+Enter / Ctrl+J)\n"
                      "  Ctrl+V / Alt+V paste an image from the clipboard (needs a vision model;\n"
                      "                 Ctrl+V works in Windows Terminal and on macOS/Linux,\n"
                      "                 Alt+V is the fallback when terminal reserves Ctrl+V)\n"
                      "  !command       run a shell command yourself (not sent to the model)\n"
                      "  #note          append a note to ./HUBBLE.md (project memory)\n"
                      "  Shift+Tab      cycle permission mode\n"
                      "  Ctrl+C         cancel the running turn · Ctrl+D quit\n"
                      "\n[bold]Skills[/bold]: .hubble/skills/<name>/SKILL.md (or ~/.hubble/skills for every "
                      "project) becomes /<name>, and the model can call it itself when its description "
                      "matches the task. /skill new <name> creates one; /skills lists them. "
                      "$ARGUMENTS is replaced with what you type after the command.")

    def cmd_exit(self, arg):
        return False

    def cmd_clear(self, arg):
        self.agent.clear()
        if self.agent.session:
            self.agent.session = self.store.new(self.agent.model)
        console.print(f"[green]New conversation.[/green] "
                      f"[dim]session {self.agent.session.id if self.agent.session else '(not saved)'}[/dim]")

    def cmd_compact(self, arg):
        with console.status("[dim]Compacting...[/dim]"):
            try:
                ok = self.agent.compact(focus=arg)
            except Exception as e:
                console.print(f"[red]Compaction failed: {escape(str(e))}[/red]")
                return
        if not ok:
            console.print("[dim]Nothing to compact.[/dim]")

    def cmd_model(self, arg):
        if not arg:
            if not sys.stdin.isatty():
                self.cmd_models("")
                return
            self.choose_model()
            return
        if arg.isdigit() and self.last_model_list:
            idx = int(arg) - 1
            if 0 <= idx < len(self.last_model_list):
                return self._set_model(self.last_model_list[idx])
            console.print(f"[red]Pick 1-{len(self.last_model_list)}[/red]")
            return
        pairs = [v for v, _, _ in self.model_items()]
        names = list(dict.fromkeys(m for _, m in pairs))
        matches = resolve_model(arg, names)
        if len(matches) == 1:
            owners = [p for p, m in pairs if m == matches[0]]
            prov = self.agent.provider_name if self.agent.provider_name in owners else owners[0]
            return self._set_model(matches[0], prov)
        if len(matches) > 1:
            self.last_model_list = matches
            for i, m in enumerate(matches, 1):
                console.print(f"  {i:>2}. {escape(m)}")
            console.print("[dim]Several match; use /model <#>.[/dim]")
            return
        self._set_model(arg)
        console.print("[dim](not in the registry; using it as given)[/dim]")

    def model_items(self, only: Optional[str] = None):
        """Picker rows across providers: value (provider, model), label, meta."""
        names = [only] if only else [self.agent.provider_name] + [n for n in self.providers
                                                                 if n != self.agent.provider_name]
        multi = len(self.providers) > 1
        items = []
        for pname in names:
            for m in provider_models(pname):
                if m.get("available") is False:
                    meta = f"unavailable · {m['category']}"
                elif m.get("available") is None and m.get("note"):
                    meta = f"unknown ({m['note']}) · {m['category']}"
                elif m.get("available") is None and pname != DEFAULT_PROVIDER:
                    meta = "not checked"
                else:
                    lat = f"{m['latency_ms']} ms" if m.get("latency_ms") else "-"
                    meta = f"{lat:>8} · {m['category']}"
                if m.get("tools") is False:
                    meta += " · no tools (chat only)"
                items.append(((pname, m["model"]), m["model"], (f"{pname} · " if multi else "") + meta))
        return items

    def choose_model(self, only: Optional[str] = None) -> bool:
        running = [f"{n}: {s.summary()}" for n, s in self.scanners.items() if s.running and (not only or n == only)]
        if running:
            console.print(f"[dim]{'; '.join(running)}. Showing what is known so far.[/dim]")
        items = self.model_items(only)
        if not items:
            console.print("[yellow]No models known for this provider yet.[/yellow]")
            return False
        title = f"Select a model ({only})" if only else "Select a model"
        chosen = pick(title, items, current=(self.agent.provider_name, self.agent.model))
        if not chosen or chosen == (self.agent.provider_name, self.agent.model):
            console.print(f"[dim]Model unchanged: {escape(self.agent.model)}[/dim]")
            return False
        self._set_model(chosen[1], chosen[0])
        return True

    def _set_model(self, name: str, provider: Optional[str] = None):
        provider = provider or self.agent.provider_name
        if provider != self.agent.provider_name:
            self.agent.provider = self.client(provider)
            self.agent.provider_name = provider
        self.agent.model = name
        self.agent.settings["model"] = name
        if self.agent.session:
            self.agent.session.meta(model=name, provider=provider)
        self._apply_known_context_window(provider, name)
        try:
            save_user_setting("model", name)
            save_user_setting("provider", provider)
            saved = " (saved as default)"
        except OSError:
            saved = ""
        prov = f"{escape(provider)}: " if len(self.providers) > 1 else ""
        console.print(f"[dim]Model set to[/dim] [bold yellow]{prov}{escape(name)}[/bold yellow][dim]{saved}[/dim]")

    def _apply_known_context_window(self, provider: str, name: str):
        """If the switched-to model publishes its real context size (most gateways don't;
        OpenRouter and a few others do), use it instead of the generic default -- unless the
        user has turned auto-detection off."""
        if self.agent.settings.get("context_window_auto", True) is False:
            return
        known = next((m.get("context_length") for m in provider_models(provider) if m.get("model") == name), None)
        if not known or known == self.agent.settings.get("context_window"):
            return
        old = self.agent.settings.get("context_window")
        self.agent.settings["context_window"] = known
        console.print(f"[dim]  context window: {old:,} -> {known:,} tokens (reported by the provider "
                      "for this model; set context_window_auto: false to keep it fixed)[/dim]")

    def cmd_models(self, arg):
        name = self.agent.provider_name
        scanner = self.scanners.get(name)
        if arg.lower() in ("refresh", "scan", "update"):
            if scanner and scanner.running:
                console.print(f"[dim]Already running: {scanner.summary()}[/dim]")
            elif not self.start_scan(name, "Refreshing."):
                console.print("[dim]Cannot check this provider.[/dim]")
            return
        age = scan_age_hours(scan_file(name))
        if scanner and scanner.running:
            console.print(f"[dim]{scanner.summary()}; the list below is from the previous check.[/dim]")
        elif age is not None:
            console.print(f"[dim]{escape(name)}: availability checked {age:.1f}h ago. "
                          "'/models refresh' re-checks now.[/dim]")
        models = [m for m in provider_models(name) if not arg or arg.lower() in m["model"].lower()]
        self.last_model_list = [m["model"] for m in models]
        table = Table(box=None, padding=(0, 2))
        table.add_column("#", justify="right", style="dim")
        table.add_column("model", overflow="fold")
        table.add_column("category", style="dim")
        table.add_column("latency", justify="right", style="dim")
        for i, m in enumerate(models, 1):
            label = f"[bold green]{escape(m['model'])}[/bold green]" if m["model"] == self.agent.model else escape(m["model"])
            if m.get("available") is False:
                lat = "[red]unavailable[/red]"
                label = f"[dim]{label}[/dim]"
            elif m.get("available") is None and m.get("note"):
                lat = f"[yellow]unknown ({escape(m['note'])})[/yellow]"
            else:
                lat = f"{m['latency_ms']} ms" if m.get("latency_ms") else "-"
            table.add_row(str(i), label, m["category"], lat)
        console.print(table)
        console.print("[dim]Switch with /model <#>. Re-check availability: /models refresh[/dim]")

    def cmd_provider(self, arg):
        sub, _, rest = arg.partition(" ")
        sub = sub.lower()
        if sub == "add":
            return self.add_provider()
        if sub in ("remove", "rm", "delete"):
            return self.remove_provider(rest.strip())
        if sub == "secure":
            from hubble import keystore
            from hubble.providers import secure_existing_keys
            if not keystore.available():
                console.print("[yellow]No OS credential store available here (e.g. headless Linux); keys stay "
                              "in ~/.hubble/providers.json. Keep that file private.[/yellow]")
                return
            moved, left = secure_existing_keys()
            console.print(f"[green]Moved {moved} API key(s) into the OS credential store.[/green]"
                          + (f" [yellow]{left} could not be moved.[/yellow]" if left else "")
                          + " [dim]The built-in provider's key comes from .env / HUBBLE_API_KEY and is not moved.[/dim]")
            return
        if sub == "list" or not sys.stdin.isatty():
            for n, cfg in self.providers.items():
                mark = "[bold green]●[/bold green]" if n == self.agent.provider_name else " "
                console.print(f" {mark} [bold]{escape(n):<14}[/bold] [dim]{escape(cfg.base_url)}[/dim]")
            console.print("[dim]/provider add · /provider remove <name> · /provider to switch[/dim]")
            return
        if sub and sub in self.providers:
            return self.choose_model(only=sub)
        items = [(n, n, cfg.base_url + ("  (active)" if n == self.agent.provider_name else ""))
                 for n, cfg in self.providers.items()]
        items.append(("__add__", "+ Add a provider", "base URL + API key"))
        chosen = pick("Provider", items, current=self.agent.provider_name)
        if chosen == "__add__":
            self.add_provider()
        elif chosen:
            self.choose_model(only=chosen)

    def add_provider(self):
        from hubble.onboarding import add_provider_wizard, _confirm
        result = add_provider_wizard(self.providers.keys())
        if not result:
            console.print("[dim]Cancelled.[/dim]")
            return
        cfg, ids = result
        register_provider(cfg, ids)
        self.providers[cfg.name] = cfg
        console.print(f"[green]Saved provider {escape(cfg.name)}[/green] [dim](~/.hubble/providers.json)[/dim]")
        if cfg.check_models or cfg.kind == "anthropic":
            self.start_scan(cfg.name, "New provider.")
        if _confirm(f"Switch to {cfg.name} now?"):
            self.choose_model(only=cfg.name)

    def remove_provider(self, name: str):
        if not name:
            console.print("Usage: /provider remove <name>")
        elif name == DEFAULT_PROVIDER:
            console.print("[yellow]The built-in hubble provider comes from HUBBLE_API_KEY / .env; remove it there.[/yellow]")
        elif name == self.agent.provider_name:
            console.print("[yellow]Switch to another provider before removing the active one.[/yellow]")
        elif remove_provider(name):
            self.providers.pop(name, None)
            self.clients.pop(name, None)
            self.scanners.pop(name, None)
            console.print(f"[green]Removed provider {escape(name)}[/green]")
        else:
            console.print(f"[red]No provider named {escape(name)}[/red]")

    def cmd_mode(self, arg):
        perms = self.agent.permissions
        if not arg:
            if not sys.stdin.isatty():
                for m in MODES:
                    mark = "[bold green]●[/bold green]" if m == perms.mode else " "
                    console.print(f" {mark} [bold]{m:<13}[/bold] [dim]{MODE_HELP[m]}[/dim]")
                return
            arg = pick("Permission mode", [(m, m, MODE_HELP[m]) for m in MODES], current=perms.mode)
            if not arg:
                return
        if arg not in MODES:
            console.print(f"[red]Unknown mode. Choose: {', '.join(MODES)}[/red]")
            return
        perms.mode = arg
        console.print(f"[green]Permission mode: {arg}[/green] [dim]({MODE_HELP[arg]})[/dim]")

    def cmd_persona(self, arg):
        if not arg and sys.stdin.isatty():
            arg = pick("Persona", [(p, p, PERSONAS[p][:70]) for p in PERSONAS], current=self.agent.persona) or ""
            if not arg:
                return
        if not arg:
            console.print(f"Persona: {self.agent.persona}. Available: {', '.join(PERSONAS)}")
        elif arg in PERSONAS:
            self.agent.persona = arg
            console.print(f"[green]Persona: {arg}[/green]")
        else:
            console.print(f"[red]Unknown persona. Available: {', '.join(PERSONAS)}[/red]")

    def cmd_resume(self, arg):
        items = self.store.list()
        if not items:
            console.print("[dim]No saved sessions for this project.[/dim]")
            return
        if not arg and sys.stdin.isatty():
            arg = pick("Resume a session", session_items(items)) or ""
            if not arg:
                return
        if not arg:
            for i, it in enumerate(items, 1):
                when = time.strftime("%m-%d %H:%M", time.localtime(it["updated"]))
                console.print(f"  {i:>2}. [dim]{when}[/dim] {escape(it['title'] or '(no prompt)')} "
                              f"[dim]{it['messages']} msgs · {escape(str(it['model']))} · {it['id']}[/dim]")
            console.print("[dim]Resume with /resume <#|id>.[/dim]")
            return
        sid = items[int(arg) - 1]["id"] if arg.isdigit() and 0 < int(arg) <= len(items) else arg
        try:
            session, messages, meta = self.store.load(sid)
        except FileNotFoundError as e:
            console.print(f"[red]{escape(str(e))}[/red]")
            return
        self.agent.session = session
        self.agent.load_history(messages)
        prov = normalize_provider_name(meta.get("provider"))
        if meta.get("model") and prov in self.providers:
            if prov != self.agent.provider_name:
                self.agent.provider = self.client(prov)
                self.agent.provider_name = prov
            self.agent.model = meta["model"]
        console.print(f"[green]Resumed {session.id}[/green] [dim]({len(messages)} messages, model {escape(self.agent.model)})[/dim]")
        print_history_tail(messages)

    def cmd_cost(self, arg):
        a = self.agent
        window = int(a.settings.get("context_window", 128000))
        subs = getattr(a, "subagent_history", [])

        if not subs:
            console.print(f"  model          {escape(a.model)}")
            console.print(f"  input tokens   {a.total_prompt_tokens:,}")
            console.print(f"  output tokens  {a.total_completion_tokens:,}")
            console.print(f"  total tokens   {(a.total_prompt_tokens + a.total_completion_tokens):,}")
            console.print(f"  context        ~{a.context_tokens:,} / {window:,} ({a.context_ratio():.0%})")
            console.print(f"  messages       {len(a.messages)}")
            return

        parent_prompt = max(0, a.total_prompt_tokens - sum(s.get("prompt_tokens", 0) for s in subs))
        parent_comp = max(0, a.total_completion_tokens - sum(s.get("completion_tokens", 0) for s in subs))

        console.print("[bold yellow]Main Agent[/bold yellow]")
        console.print(f"  model          {escape(a.model)}")
        console.print(f"  input tokens   {parent_prompt:,}")
        console.print(f"  output tokens  {parent_comp:,}")
        console.print(f"  context        ~{a.context_tokens:,} / {window:,} ({a.context_ratio():.0%})")
        console.print(f"  messages       {len(a.messages)}")

        console.print(f"\n[bold green]Subagents ({len(subs)})[/bold green]")
        tbl = Table.grid(padding=(0, 2))
        tbl.add_column(style="bold", no_wrap=True)                  # name/label
        tbl.add_column(style="dim yellow", no_wrap=True)               # model
        tbl.add_column(justify="right", no_wrap=True)               # input tokens
        tbl.add_column(justify="right", no_wrap=True)               # output tokens
        tbl.add_column(justify="right", no_wrap=True)               # context
        tbl.add_column(justify="right", style="dim", no_wrap=True)  # status/tools

        tbl.add_row("Agent / Task", "Model", "Input (Prompt)", "Output (Compl)", "Context", "Status")
        for s in subs:
            ctx_ratio = (s['context_tokens'] / s['context_window']) if s.get('context_window') else 0
            ctx_str = f"~{s['context_tokens']:,} ({ctx_ratio:.0%})" if s.get('context_tokens') else "-"
            tbl.add_row(
                f"• {escape(s.get('label', 'task')[:32])}",
                escape(s.get('model', a.model)),
                f"{s.get('prompt_tokens', 0):,}",
                f"{s.get('completion_tokens', 0):,}",
                ctx_str,
                f"{s.get('status', 'done')} ({s.get('tool_calls', 0)} tools)",
            )
        console.print(tbl)

        console.print(f"\n[bold green]Session Total (Main + Subagents)[/bold green]")
        console.print(f"  total input    {a.total_prompt_tokens:,}")
        console.print(f"  total output   {a.total_completion_tokens:,}")
        console.print(f"  combined       {(a.total_prompt_tokens + a.total_completion_tokens):,}")

    def cmd_context(self, arg):
        a = self.agent
        from hubble.agent import estimate_messages, estimate_tokens
        sys_tokens = estimate_tokens(a.system_prompt())
        tool_tokens = estimate_tokens(str([t.schema() for t in a.tools]))
        console.print(f"  system prompt  ~{sys_tokens:,} tokens "
                      f"(memory: {len(a.memory)} file(s), pinned: {len(a.pinned)})")
        console.print(f"  tool schemas   ~{tool_tokens:,} tokens ({len(a.tools)} tools)")
        console.print(f"  messages       ~{estimate_messages(a.messages):,} tokens ({len(a.messages)} messages)")
        subs = getattr(a, "subagent_history", [])
        if subs:
            console.print(f"\n[bold]Subagent Contexts ({len(subs)} executed)[/bold]")
            for s in subs:
                ctx_ratio = (s['context_tokens'] / s['context_window']) if s.get('context_window') else 0
                console.print(f"  • {escape(s.get('label', 'task'))} [dim]({escape(s.get('model', ''))})[/dim]: "
                              f"~{s.get('context_tokens', 0):,} / {s.get('context_window', 0):,} tokens ({ctx_ratio:.0%})")

    def cmd_undo(self, arg):
        restored = self.agent.ctx.undo()
        if not restored:
            console.print("[dim]No file changes to undo.[/dim]")
            return
        for r in restored:
            console.print(f"  [yellow]reverted[/yellow] {escape(r)}")
        note = "The user reverted your last file changes (/undo): " + ", ".join(restored)
        self.agent.add_user_message(f"[{note}]")
        self.agent._append({"role": "assistant", "content": "Noted; those changes were reverted."})

    def cmd_diff(self, arg):
        try:
            res = subprocess.run(["git", "diff", "--stat"] + ([arg] if arg else []), cwd=self.agent.ctx.root,
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
            full = subprocess.run(["git", "diff"] + ([arg] if arg else []), cwd=self.agent.ctx.root,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            console.print(f"[red]{escape(str(e))}[/red]")
            return
        if res.returncode != 0:
            console.print(f"[red]{escape(res.stderr.strip() or 'git diff failed')}[/red]")
            return
        if not full.stdout.strip():
            console.print("[dim]No unstaged changes.[/dim]")
            return
        from hubble.ui import render_diff
        console.print(render_diff(full.stdout, max_lines=400))
        console.print(f"[dim]{escape(res.stdout.strip())}[/dim]")

    def cmd_init(self, arg):
        self.send(INIT_PROMPT + (f"\n\nExtra instructions: {arg}" if arg else ""))
        self.agent.reload_memory()

    def cmd_memory(self, arg):
        if not self.agent.memory:
            console.print("[dim]No memory files. Create one with /init or add notes with #note.[/dim]")
        for p, text in self.agent.memory:
            console.print(f"  [bold]{escape(str(p))}[/bold] [dim]({len(text.splitlines())} lines)[/dim]")

    def cmd_lens(self, arg):
        """Show, initialize, or update developer lens and design language (LENS.md)."""
        from hubble.prompts import LENS_TEMPLATE, find_lens_files
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        target_file = self.agent.ctx.root / "LENS.md"

        if sub in ("init", "create", "new"):
            if target_file.exists():
                console.print(f"[yellow]{escape(str(target_file))} already exists. Edit it or use '/lens add <preference>'.[/yellow]")
                return
            target_file.write_text(LENS_TEMPLATE, encoding="utf-8")
            self.agent.reload_memory()
            console.print(f"[green]Created {escape(str(target_file))} with design language and coding style guidelines.[/green]")
            console.print(f"[green]Developer Lens updated: {len(self.agent.lens)} active configuration(s).[/green]")
            console.print("[dim]Edit LENS.md or add rules via '/lens add <preference>'.[/dim]")
            return

        if sub == "add":
            if not rest:
                console.print("[yellow]Usage: /lens add <preference>[/yellow]\nExample: /lens add Prefer Tailwind CSS with zinc palette and no inline styles.")
                return
            if not target_file.exists():
                target_file.write_text(LENS_TEMPLATE + f"\n- {rest}\n", encoding="utf-8")
            else:
                existing = target_file.read_text(encoding="utf-8")
                target_file.write_text(existing.rstrip("\n") + f"\n- {rest}\n", encoding="utf-8")
            self.agent.reload_memory()
            console.print(f"[green]Saved preference to {escape(str(target_file))}[/green]")
            console.print(f"[green]Developer Lens updated: {len(self.agent.lens)} active configuration(s).[/green]")
            return

        lens_files = find_lens_files(self.agent.ctx.root)
        active_lens = getattr(self.agent, "lens", None) or getattr(self.agent, "taste", [])
        if not lens_files and not active_lens:
            console.print("[bold]Developer Lens & Design Language:[/bold] [dim](none configured)[/dim]")
            console.print("Hubble interprets your codebase and styling through your personal Lens:")
            console.print("  [bold]/lens init[/bold]         create a starter LENS.md in workspace")
            console.print("  [bold]/lens add <rule>[/bold]    append a style, aesthetic, or design preference")
            console.print("  Global lens: put [bold]~/.hubble/LENS.md[/bold] to apply your lens across all projects.")
            return

        console.print("[bold]Active Developer Lens & Design Language:[/bold]")
        for p, text in active_lens:
            lines = text.splitlines()
            console.print(f"  [bold yellow]{escape(str(p))}[/bold yellow] [dim]({len(lines)} lines)[/dim]")
            preview = [line for line in lines if line.strip() and not line.startswith("#")][:6]
            for pl in preview:
                console.print(f"    [dim]{escape(pl)}[/dim]")
            if len(lines) > 8:
                console.print(f"    [dim]... ({len(lines) - 6} more lines)[/dim]")
        console.print("\n[dim]Commands: /lens add <rule> | /lens init[/dim]")

    cmd_taste = cmd_lens

    def cmd_skills(self, arg):
        if not self.agent.skills:
            console.print("[dim]No skills yet. Ask the model to save one, or /skill new <name>.[/dim]")
            return
        for s in self.agent.skills:
            console.print(f"  [bold]/{s.name}[/bold] [dim]({s.scope})[/dim]  {escape(s.description)}")
        console.print("[dim]Run one with /<name>, or the model calls them on its own when relevant.[/dim]")

    def cmd_stats(self, arg):
        from hubble import stats
        try:
            days = float(arg) if arg.strip() else 30.0
        except ValueError:
            console.print("Usage: /stats [days]  (default 30)")
            return
        rows = stats.summarize(stats.load(days=days), self.agent.settings.get("model_prices"))
        console.print(stats.render(rows, days))

    def cmd_route(self, arg):
        from hubble import stats
        from hubble.router import parse_spec, routing_config
        s = self.agent.settings
        cfg = dict(s.get("routing") or {})
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        cur = f"{self.agent.provider_name}:{self.agent.model}"

        def save():
            s["routing"] = cfg
            save_user_setting("routing", cfg)

        if sub in ("fast", "strong") and len(parts) == 2:
            spec = parts[1].strip()
            prov, model = parse_spec(spec, self.agent.provider_name)
            if prov not in self.providers:
                console.print(f"[red]Unknown provider '{escape(prov)}'. Use provider:model, e.g. hubble:codestral-latest[/red]")
                return
            cfg[sub] = f"{prov}:{model}"
            cfg.setdefault("strong" if sub == "fast" else "fast", cur)
            cfg["enabled"] = True
            save()
        elif sub == "auto":
            rows = stats.summarize(stats.load(days=30), s.get("model_prices"))
            fast = stats.best_fast_model(rows, exclude=[cur, self.agent.model])
            if not fast:
                console.print("[yellow]Not enough history yet to pick a fast model (needs a model with 5+ calls at "
                              "90%+ success). Use /route fast <model>.[/yellow]")
                return
            cfg.update(fast=f"{fast['provider']}:{fast['model']}", strong=cfg.get("strong") or cur, enabled=True)
            save()
            console.print(f"[dim]Picked the fastest reliable model you have used: {escape(fast['model'])} "
                          f"({fast['tok_per_s']:.0f} tok/s, {fast['call_success']:.0%} of {fast['calls']} calls ok).[/dim]")
        elif sub in ("off", "on"):
            cfg["enabled"] = sub == "on"
            save()
        elif sub:
            console.print("Usage: /route [fast <model> | strong <model> | auto | on | off]")
            return
        active = routing_config(s)
        if not active:
            state = "off" if cfg.get("fast") and cfg.get("strong") else "not set up"
            console.print(f"[yellow]Routing: {state}.[/yellow] [dim]Set it with /route fast <model> and "
                          "/route strong <model>, or /route auto to pick the fast model from your /stats.[/dim]")
            return
        console.print(f"[green]Routing on[/green]  fast [bold]{escape(active['fast'])}[/bold]  ·  "
                      f"strong [bold]{escape(active['strong'])}[/bold]")
        console.print("[dim]Questions, lookups and short messages → fast; changes, fixes, debugging, long or "
                      "code-heavy prompts → strong. Research sub-agents use fast. A fast task that struggles "
                      "switches to strong. /route off to stop.[/dim]")
        if self.agent.last_route:
            tier, prov, model, reasons = self.agent.last_route
            console.print(f"[dim]Last prompt → {tier} ({escape(model)}): {escape('; '.join(reasons))}[/dim]")

    def cmd_team(self, arg):
        """The models sub-agents can use; the main model sees this list and picks per task."""
        from hubble.router import parse_spec
        s = self.agent.settings
        team = [e if isinstance(e, dict) else {"model": str(e), "use": ""} for e in (s.get("subagent_models") or [])]
        parts = arg.split(maxsplit=2)
        sub = parts[0].lower() if parts else ""

        def save():
            s["subagent_models"] = team
            save_user_setting("subagent_models", team)

        if sub == "add" and len(parts) >= 2:
            prov, model = parse_spec(parts[1], self.agent.provider_name)
            if prov not in self.providers:
                console.print(f"[red]Unknown provider '{escape(prov)}'. Use provider:model, e.g. hubble:codestral-latest[/red]")
                return
            spec = f"{prov}:{model}"
            team = [e for e in team if e["model"] != spec] + [{"model": spec, "use": parts[2] if len(parts) > 2 else ""}]
            save()
        elif sub in ("remove", "rm") and len(parts) >= 2:
            key = parts[1]
            before = len(team)
            if key.isdigit() and 1 <= int(key) <= len(team):
                team.pop(int(key) - 1)
            else:
                prov, model = parse_spec(key, self.agent.provider_name)
                team = [e for e in team if e["model"] not in (key, f"{prov}:{model}")]
            if len(team) == before:
                console.print(f"[yellow]No team member {escape(key)}.[/yellow]")
                return
            save()
        elif sub == "clear":
            team = []
            save()
        elif sub:
            console.print("Usage: /team [add <provider:model> [what it is good for] | remove <#|model> | clear]")
            return
        if not team:
            console.print("[dim]No sub-agent team yet: sub-agents use your current model (or the fast routing tier "
                          "for research). Add models with, e.g.:\n"
                          "  /team add hubble:ministral-8b-latest fast, for searching and reading lots of files\n"
                          "  /team add anthropic:claude-opus-5 careful, for tricky changes and code review[/dim]")
            return
        from hubble.agent import team_block
        console.print("[bold]Sub-agent model team[/bold]")
        for i, e in enumerate(team, 1):
            console.print(f"  {i}. [bold #ffd75f]{escape(e['model'])}[/bold #ffd75f]"
                          + (f"  [dim]{escape(e['use'])}[/dim]" if e.get("use") else ""))
        console.print("[dim]The main model picks from this list for each sub-agent, by what the part of the job "
                      "needs; say so in your prompt to steer it (\"use the fast model to search, opus to review\"). "
                      "Each sub-agent's model shows next to it while it runs. /stats shows how each one did.[/dim]")
        if "[" in team_block(s):
            console.print("[dim]Their recent record (from /stats) is shown to the main model too.[/dim]")

    def cmd_install_github(self, arg):
        from hubble.github import install_workflow
        path = self.agent.ctx.root / ".github" / "workflows" / "hubble.yml"
        if path.exists() and arg.strip() != "--force":
            console.print(f"[yellow]{escape(str(path))} already exists.[/yellow] [dim]/install-github --force "
                          "overwrites it.[/dim]")
            return
        install_workflow(self.agent.ctx.root)
        console.print(f"[green]Wrote {escape(self.agent.ctx.rel(path))}.[/green]\n"
                      "[dim]Next:\n"
                      "  1. Add your API key as a repository secret named HUBBLE_API_KEY\n"
                      "     (Settings → Secrets and variables → Actions), e.g. gh secret set HUBBLE_API_KEY\n"
                      "  2. Optional repository variables: HUBBLE_MODEL, HUBBLE_BASE_URL\n"
                      "  3. Commit and push the workflow.\n"
                      "Then every PR gets a review, and \"@hubble <request>\" in a comment gets an answer "
                      "(on a PR it can push fixes). Only owners, members and collaborators can trigger it.[/dim]")

    def cmd_worktree(self, arg):
        from hubble import worktree
        parts = arg.split()
        sub = parts[0] if parts else "list"
        ctx = self.agent.ctx
        try:
            if sub in ("new", "create", "switch", "enter") and len(parts) >= 2:
                name = parts[1]
                existing = next((w for w in worktree.list_worktrees(ctx.root) if w["name"] == name), None)
                if existing is None:
                    if sub in ("switch", "enter"):
                        console.print(f"[yellow]No worktree named {escape(name)}. /worktree new {escape(name)}[/yellow]")
                        return
                    info = worktree.create(ctx.root, name)
                    console.print(f"[green]Created worktree {escape(info['path'])}[/green] [dim](branch "
                                  f"{escape(info['branch'])} from {escape(info['base'])})[/dim]")
                    path = Path(info["path"])
                else:
                    path = Path(existing["path"])
                self._switch_root(path)
            elif sub in ("exit", "leave", "main"):
                if not self._main_root or ctx.root == self._main_root:
                    console.print("[dim]Already in the main checkout.[/dim]")
                    return
                self._switch_root(self._main_root)
            elif sub in ("remove", "rm", "delete") and len(parts) >= 2:
                if ctx.root.name == parts[1] and self._main_root:
                    self._switch_root(self._main_root)
                gone = worktree.remove(ctx.root, parts[1], force="--force" in parts,
                                       delete_branch="--delete-branch" in parts)
                console.print(f"[green]Removed worktree {escape(gone)}.[/green]")
            elif sub == "list":
                items = worktree.list_worktrees(ctx.root)
                if not items:
                    console.print("[dim]Not a git repository.[/dim]")
                    return
                for i, w in enumerate(items):
                    here = " [bold green]●[/bold green]" if Path(w["path"]) == ctx.root else "  "
                    label = w["name"] if w["hubble"] else ("(main)" if i == 0 else Path(w["path"]).name)
                    console.print(f"{here} [bold]{escape(label)}[/bold] [dim]{escape(w['branch'])} "
                                  f"{escape(w['head'])}  {escape(w['path'])}[/dim]")
                console.print("[dim]/worktree new <name> · switch <name> · exit · remove <name> [--force] "
                              "[--delete-branch][/dim]")
            else:
                console.print("Usage: /worktree [list | new <name> | switch <name> | exit | remove <name>]")
        except worktree.WorktreeError as e:
            console.print(f"[red]{escape(str(e))}[/red]")

    def cmd_orchestrate(self, arg):
        from hubble.orchestration.coordinator import Coordinator
        from hubble.orchestration.graph import Task, TaskGraph
        from hubble.orchestration.scheduler import Scheduler
        from hubble.runtimes import default_registry
        from hubble.runs.store import RunStore
        parts = arg.split()
        sub = parts[0].lower() if parts else "runtimes"
        registry = default_registry(include_fake=True)
        if sub in ("runtimes", "list", "ls"):
            for det in registry.detect_all():
                state = "[green]available[/green]" if det.available else "[red]unavailable[/red]"
                diag = f" [dim]{escape('; '.join(det.diagnostics))}[/dim]" if det.diagnostics else ""
                console.print(f"  {state} [bold]{escape(det.runtime_id)}[/bold]"
                              f" [dim]{escape(det.version)}[/dim]{diag}")
            return
        if sub == "run":
            rest = arg[len(parts[0]):].strip()
            runtime = ""
            if "--runtime" in rest:
                rest, _, runtime = rest.partition("--runtime")
                runtime = runtime.strip().split()[0] if runtime.strip() else ""
            task_id, _, objective = rest.partition(":")
            if not objective.strip():
                console.print("Usage: /orchestrate run <id>:<objective> [--runtime <name>]")
                return
            root = self.agent.ctx.root
            coordinator = Coordinator(root, registry)
            store = RunStore(root / ".hubble" / "runs.jsonl")
            graph = TaskGraph([Task(task_id=task_id.strip(), objective=objective.strip(),
                                    runtime_id=runtime, required_capabilities=["json_stream"])])
            with console.status("Orchestrating…"):
                result = Scheduler(graph, coordinator.execute, store=store).run()
            for tid, status in result.items():
                color = "green" if status == "succeeded" else "red"
                console.print(f"  [{color}]{escape(tid)}: {escape(status)}[/{color}]")
            console.print("[dim]Diffs stay in their worktree until you merge them by hand.[/dim]")
            return
        console.print("Usage: /orchestrate [runtimes | run <id>:<objective> [--runtime <name>]]")

    def _switch_root(self, path: Path):
        """Point every tool at another checkout. History stays; the system prompt shows the new root."""
        ctx = self.agent.ctx
        if self._main_root is None:
            self._main_root = ctx.root
        ctx.root = path.resolve()
        ctx.read_mtimes.clear()
        self.agent.reload_memory()
        self.agent.reload_skills()
        self.agent.reload_agents()
        from hubble import worktree
        branch = worktree.current_branch(ctx.root)
        console.print(f"[green]Working in {escape(str(ctx.root))}[/green] [dim](branch {escape(branch)}). "
                      "/worktree exit returns to the main checkout.[/dim]")

    def cmd_agents(self, arg):
        from hubble.subagents import BUILTIN_AGENTS, save_agent_definition
        parts = arg.split()
        subcmd = parts[0].lower() if parts else ""

        all_defs = dict(BUILTIN_AGENTS)
        for a in (self.agent.agent_defs or []):
            all_defs[a.name] = a

        if not subcmd:
            if not all_defs:
                console.print("[dim]No agents available. /agent new creates one.[/dim]")
                return

            console.print("\n[bold]Specialist Agents:[/bold]")
            for a in sorted(all_defs.values(), key=lambda x: x.name):
                color = a.color if a.color in ("yellow", "green", "orange", "cyan", "magenta", "blue", "red", "purple") else "yellow"
                props = [a.capability]
                if getattr(a, "fork_context", False):
                    props.append("forks context")
                if a.model:
                    props.append(f"model {a.model}")
                if a.tools:
                    props.append(f"tools: {', '.join(a.tools)}")
                extra_str = ", ".join(props)
                console.print(f"  [{color}][bold]{escape(a.name)}[/bold][/{color}] [dim]({escape(a.scope)}; {escape(extra_str)})[/dim]")
                console.print(f"    {escape(a.description)}")

            console.print("\n[dim]Commands: /agent new (create agent) · /agent fork <name> (fork session) · /agent <name>[/dim]")
            console.print("[dim]The model picks one with the task tool; or ask: \"use the <name> agent to ...\"[/dim]\n")
            return

        if subcmd in ("new", "create", "add"):
            name = parts[1].lower() if len(parts) > 1 else ""
            scope = parts[2].lower() if len(parts) > 2 and parts[2].lower() in ("user", "project") else ""
            desc = ""
            prompt = ""
            capability = "read_only"
            color = "yellow"
            fork_context = False

            try:
                while not name or not re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", name):
                    if name:
                        console.print("[yellow]Invalid name. Use lowercase letters, digits, - or _.[/yellow]")
                    name = console.input("[bold yellow]Agent name[/bold yellow] (lowercase letters, digits, - or _): ").strip().lower()
                    if not name:
                        console.print("[yellow]Agent creation cancelled.[/yellow]")
                        return

                desc = console.input(f"[bold yellow]Description[/bold yellow] (what '{name}' does / when to use): ").strip()
                if not desc:
                    desc = f"Specialist sub-agent for {name}"

                prompt = console.input(f"[bold yellow]Role / system prompt[/bold yellow]: ").strip()
                if not prompt:
                    prompt = f"You are a specialist {name} sub-agent. Implement the assigned task thoroughly and report back."

                cap_choice = console.input("[bold yellow]Capability[/bold yellow] [1. read_only, 2. edit] (default: 1): ").strip().lower()
                capability = "edit" if cap_choice in ("2", "edit") else "read_only"

                color_choice = console.input("[bold yellow]Color selection[/bold yellow] (green, yellow, orange, red) (default: yellow): ").strip().lower()
                valid_colors = ("yellow", "green", "orange", "red", "cyan", "magenta", "blue", "purple")
                color = color_choice if color_choice in valid_colors else "yellow"

                fork_choice = console.input("[bold yellow]Fork parent context by default?[/bold yellow] [yes / no] (default: no): ").strip().lower()
                fork_context = fork_choice in ("yes", "y", "true", "1")

                if not scope:
                    scope_choice = console.input("[bold yellow]Scope[/bold yellow] [project / user] (default: project): ").strip().lower()
                    scope = "user" if scope_choice.startswith("u") else "project"

            except (EOFError, KeyboardInterrupt):
                if not name or not re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", name):
                    console.print("\n[yellow]Agent creation cancelled.[/yellow]")
                    return
                desc = desc or f"Specialist sub-agent for {name}"
                prompt = prompt or f"You are a specialist {name} sub-agent."
                capability = capability or "read_only"
                color = color or "yellow"
                scope = scope or "project"

            agent_def = save_agent_definition(
                root=self.agent.ctx.root,
                name=name,
                description=desc,
                prompt=prompt,
                capability=capability,
                color=color,
                fork_context=fork_context,
                scope=scope,
            )
            self.agent.reload_agents()
            console.print(f"[green]Successfully created agent '[bold]{escape(name)}[/bold]' ({scope} scope)![/green]")
            console.print(f"[dim]Saved to {escape(str(agent_def.path))}[/dim]")
            return

        if subcmd == "fork":
            if len(parts) < 2:
                console.print("Usage: /agent fork <name> [project|user]")
                return
            name = parts[1].lower()
            if not re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", name):
                console.print("[red]Invalid name. Use lowercase letters, digits, - or _.[/red]")
                return
            scope = parts[2].lower() if len(parts) > 2 and parts[2].lower() == "user" else "project"

            last_user_prompt = ""
            for m in reversed(getattr(self.agent, "messages", [])):
                if m.get("role") == "user":
                    content = m.get("content", "")
                    if isinstance(content, str) and content.strip():
                        last_user_prompt = content.strip()
                        break

            focus_desc = (last_user_prompt[:80] + "...") if len(last_user_prompt) > 80 else last_user_prompt
            description = f"Forked session specialist for: {focus_desc}" if focus_desc else f"Forked specialist agent '{name}'"
            prompt = (
                f"You are a specialized '{name}' sub-agent forked directly from the active session.\n"
                f"You have full inherited context of the parent conversation history.\n"
            )
            if last_user_prompt:
                prompt += f"\nParent session focus:\n{last_user_prompt}\n"
            prompt += "\nExecute assigned tasks with deep context awareness and report back."

            agent_def = save_agent_definition(
                root=self.agent.ctx.root,
                name=name,
                description=description,
                prompt=prompt,
                capability="edit",
                color="green",
                fork_context=True,
                scope=scope,
            )
            self.agent.reload_agents()
            console.print(f"[green]Created forked specialist agent '[bold]{escape(name)}[/bold]' (fork_context: true)![/green]")
            console.print(f"[dim]Saved to {escape(str(agent_def.path))} · Full session history will be forked when invoked.[/dim]")
            return

        if subcmd in all_defs:
            a = all_defs[subcmd]
            color = a.color if a.color in ("yellow", "green", "orange", "cyan", "magenta", "blue", "red", "purple") else "yellow"
            console.print(f"[{color}][bold]{escape(a.name)}[/bold][/{color}] [dim]({escape(a.scope)})[/dim]")
            console.print(f"  Description:  {escape(a.description)}")
            console.print(f"  Capability:   {escape(a.capability)}")
            console.print(f"  Color:        {escape(a.color or 'default')}")
            console.print(f"  Fork Context: {a.fork_context}")
            if a.model:
                console.print(f"  Model:        {escape(a.model)}")
            if a.tools:
                console.print(f"  Tools:        {', '.join(a.tools)}")
            console.print(f"\n[bold]System Prompt:[/bold]\n{escape(a.prompt)}\n")
            return

        console.print(f"[yellow]Unknown agent or subcommand: {escape(subcmd)}[/yellow]")
        console.print("[dim]Usage: /agent [new|create|add|fork <name>|<name>][/dim]")

    def cmd_plugin(self, arg):
        from hubble import plugins
        parts = arg.split()
        root = self.agent.ctx.root
        sub = parts[0] if parts else "list"
        try:
            if sub == "install" and len(parts) >= 2:
                scope = "project" if "--project" in parts else "user"
                info = plugins.install(parts[1], root, scope)
                bits = [f"hooks: {', '.join(info['hooks'])}" if info["hooks"] else "",
                        f"MCP servers: {', '.join(info['mcp_servers'])}" if info["mcp_servers"] else ""]
                console.print(f"[green]Installed plugin {escape(info['name'])} {escape(info['version'])}[/green] "
                              f"[dim]({escape(str(info['path']))}{'; ' + '; '.join(b for b in bits if b) if any(bits) else ''})."
                              " Restart hubble to load it.[/dim]")
            elif sub in ("remove", "uninstall") and len(parts) >= 2:
                ok = plugins.remove(parts[1], root)
                console.print(f"[green]Removed {escape(parts[1])}.[/green] [dim]Restart to unload it.[/dim]" if ok
                              else f"[yellow]No plugin named {escape(parts[1])}.[/yellow]")
            elif sub in ("enable", "disable") and len(parts) >= 2:
                plugins.set_enabled(parts[1], sub == "enable")
                console.print(f"[green]{escape(parts[1])} {sub}d.[/green] [dim]Restart to apply.[/dim]")
            elif sub == "list":
                items = plugins.installed_plugins(root, include_disabled=True)
                if not items:
                    console.print("[dim]No plugins. /plugin install <folder|git-url> [--project][/dim]")
                for p in items:
                    state = "" if p["enabled"] else " [yellow]disabled[/yellow]"
                    console.print(f"  [bold]{escape(p['name'])}[/bold] {escape(p['version'])} [dim]({p['scope']})"
                                  f"[/dim]{state}  {escape(p['description'])}")
            else:
                console.print("Usage: /plugin [list | install <folder|git-url> [--project] | remove <name> | "
                              "enable <name> | disable <name>]")
        except plugins.PluginError as e:
            console.print(f"[red]{escape(str(e))}[/red]")

    def cmd_skill_new(self, arg):
        parts = arg.split()
        if not parts or parts[0] != "new" or len(parts) < 2:
            console.print("Usage: /skill new <name> [user]  (default scope: this project)")
            return
        name, scope = parts[1].lower(), ("user" if len(parts) > 2 and parts[2] == "user" else "project")
        if not re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", name):
            console.print("[red]Name must be lowercase letters, digits, - or _ (max 41 chars).[/red]")
            return
        base = HOME_DIR / "skills" if scope == "user" else self.agent.ctx.root / ".hubble" / "skills"
        path = base / name / "SKILL.md"
        if path.exists():
            console.print(f"[yellow]Already exists: {escape(str(path))}[/yellow]")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE.format(name=name, title=name.replace("-", " ").title()), encoding="utf-8")
        self.agent.reload_skills()
        self._load_custom_commands()
        console.print(f"[green]Created {escape(str(path))}[/green] [dim]— fill in its description and "
                      f"instructions, then use it as /{name}.[/dim]")

    def cmd_add(self, arg):
        if not arg:
            console.print("Usage: /add <file>")
            return
        try:
            p = self.agent.ctx.resolve(arg)
        except Exception as e:
            console.print(f"[red]{escape(str(e))}[/red]")
            return
        if not p.is_file():
            console.print(f"[red]Not a file: {escape(arg)}[/red]")
            return
        if is_secret_path(p) and not self.agent.ctx.allow_secrets:
            console.print("[red]Refusing to pin a secrets file.[/red]")
            return
        rel = self.agent.ctx.rel(p)
        self.agent.pinned[rel] = p.read_text(encoding="utf-8", errors="replace")
        console.print(f"[green]Pinned {escape(rel)}[/green] [dim]({len(self.agent.pinned[rel]):,} chars)[/dim]")

    def cmd_drop(self, arg):
        keys = [k for k in self.agent.pinned if arg and arg.replace("\\", "/") in k]
        if not keys:
            console.print("[red]Not pinned.[/red]")
            return
        for k in keys:
            del self.agent.pinned[k]
            console.print(f"[green]Unpinned {escape(k)}[/green]")

    def cmd_files(self, arg):
        if not self.agent.pinned:
            console.print("[dim]No pinned files. Use /add <file> or mention @file in a message.[/dim]")
        for k, v in self.agent.pinned.items():
            console.print(f"  {escape(k)} [dim]({len(v.splitlines())} lines)[/dim]")

    def cmd_todos(self, arg):
        if not self.agent.ctx.todos:
            console.print("[dim]No todos.[/dim]")
        render_todos(self.agent.ctx.todos)

    def cmd_test(self, arg):
        model = arg or self.agent.model
        with console.status(f"[dim]Testing {escape(model)}...[/dim]"):
            res = self.agent.provider.ping(model)
        if res["ok"]:
            console.print(f"[green]✔ {escape(model)} OK[/green] [dim]{res['latency_ms']} ms · {escape(res['msg'])}[/dim]")
        else:
            console.print(f"[red]✘ {escape(model)} failed[/red] [dim]{res['latency_ms']} ms · {escape(res['msg'])}[/dim]")

    def cmd_theme(self, arg: str = ""):
        from hubble.theme import COLOR_SCHEMES, ORDERED_SCHEMES, choose_color_scheme
        from hubble.ui import apply_theme
        from hubble.settings import save_user_setting

        target = arg.strip().lower()
        if target:
            if target in COLOR_SCHEMES:
                chosen = target
            else:
                matches = [s for s in ORDERED_SCHEMES if target in s]
                if len(matches) == 1:
                    chosen = matches[0]
                else:
                    console.print(f"[red]Unknown color scheme '{escape(arg)}'.[/red]")
                    console.print(f"[dim]Available: {', '.join(ORDERED_SCHEMES)}[/dim]")
                    return
        else:
            if not sys.stdin.isatty():
                console.print(f"Available schemes: {', '.join(ORDERED_SCHEMES)}")
                return
            current = self.agent.settings.get("theme", "terminal")
            chosen = choose_color_scheme(current=current)
            if not chosen:
                return

        self.agent.settings["theme"] = chosen
        save_user_setting("theme", chosen)
        apply_theme(chosen)
        from prompt_toolkit.styles import Style
        from hubble.theme import get_prompt_style
        if hasattr(self, "session") and self.session:
            self.session.style = Style.from_dict(get_prompt_style(chosen))
        console.print(f"[green]✔ Switched color scheme to [bold]{escape(chosen)}[/bold][/green]")

    def cmd_logo(self, arg: str = ""):
        from hubble.settings import save_user_setting
        target = arg.strip().lower()
        if not target:
            current = self.agent.settings.get("logo", "regular")
            console.print(f"Current logo: [bold yellow]{escape(current)}[/bold yellow]. Available: [bold]regular[/bold] (clean bold wordmark), [bold]block[/bold] (classic 6-row ANSI Shadow block letters), [bold]small[/bold] (1-line minimal wordmark).")
            console.print("[dim]Usage: /logo [regular|block|small][/dim]")
            return
        if target in ("regular", "default", "normal", "word", "wordmark"):
            chosen = "regular"
            label = "regular (clean bold wordmark: ▲ HUBBLE)"
        elif target in ("block", "ascii", "ansi"):
            chosen = "block"
            label = "block (classic 6-row ANSI Shadow block letters)"
        elif target in ("small", "compact", "mini", "one-line"):
            chosen = "small"
            label = "small (1-line minimal wordmark: ▲ Hubble)"
        elif target in ("big", "pixel", "large"):
            chosen = "big"
            label = "big (9-row pixel letters)"
        else:
            console.print(f"[red]Unknown logo style '{escape(arg)}'.[/red] Choose [bold]regular[/bold], [bold]block[/bold], or [bold]small[/bold].")
            return
        self.agent.settings["logo"] = chosen
        save_user_setting("logo", chosen)
        self._home_frame = None
        console.print(f"[green]✔ Logo set to {label}.[/green]")

    def cmd_thinking(self, arg):
        ev = self.agent.events
        arg = (arg or "").strip().lower()
        if arg in ("view", "inspect", "show"):
            thought = getattr(ev, "last_thinking", "") or ""
            if not thought and getattr(ev, "accordion", None):
                thought = ev.accordion.get_full_text()
            thought = thought.strip()
            if not thought:
                console.print("[dim]No reasoning text captured in the last turn.[/dim]")
                return
            from rich import box
            from rich.panel import Panel
            from rich.text import Text
            from hubble.theme import get_palette, _to_rich_style
            from hubble.ui import ACTIVE_THEME

            pal = get_palette(ACTIVE_THEME)
            title = _to_rich_style(pal.thought_title) or "bold yellow"
            text_style = _to_rich_style(pal.thought_text) or "dim"
            border_color = "yellow"
            for token in title.split():
                if token not in ("bold", "italic", "underline", "dim", "reverse"):
                    border_color = token
                    break
            console.print(Panel(
                Text(thought, style=text_style),
                title=f"[{title}] Thinking / Chain of Thought [/{title}]",
                title_align="left",
                border_style=border_color,
                box=box.ROUNDED,
                padding=(0, 1),
            ))
            return
        if arg in ("on", "off"):
            ev.show_reasoning = arg == "on"
        else:
            ev.show_reasoning = not getattr(ev, "show_reasoning", False)
        state = "shown live" if ev.show_reasoning else "collapsed accordion (use /thinking view to inspect)"
        save_user_setting("show_reasoning", ev.show_reasoning)
        console.print(f"[green]Reasoning display: {state}.[/green]")

    def cmd_multiline(self, arg):
        arg = (arg or "").strip().lower()
        if arg in ("on", "1", "true"):
            self.multiline_mode = True
        elif arg in ("off", "0", "false"):
            self.multiline_mode = False
        else:
            self.multiline_mode = not getattr(self, "multiline_mode", False)
        mode = "enabled (Enter adds newline, Esc+Enter submits)" if self.multiline_mode else "disabled (Enter submits, Shift+Enter adds newline)"
        console.print(f"[green]Multiline mode {mode}.[/green]")

    def cmd_sandbox(self, arg):
        from hubble.sandbox import effective_mode, native_kind, why_unavailable
        ctx = self.agent.ctx
        arg = arg.lower().strip()
        if arg == "docker":
            if not shutil.which("docker"):
                console.print("[red]docker was not found on PATH. Install Docker Desktop first.[/red]")
                return
            ctx.sandbox = "docker"
        elif arg in ("on", "native"):
            if not native_kind():
                console.print(f"[red]No OS sandbox on this machine: {escape(why_unavailable())}.[/red]")
                return
            ctx.sandbox = "native"
        elif arg in ("auto", "off"):
            ctx.sandbox = arg
        elif arg:
            console.print("Usage: /sandbox [auto|native|docker|off]")
            return
        if arg:
            self.agent.settings["shell_sandbox"] = ctx.sandbox
            save_user_setting("shell_sandbox", ctx.sandbox)
        mode = effective_mode(ctx.sandbox)
        net = "with network access" if ctx.sandbox_network else "network disabled"
        if mode == "docker":
            console.print(f"[green]Sandbox: docker[/green] [dim](image {ctx.sandbox_image}, "
                          f"{ctx.sandbox_memory} mem, {ctx.sandbox_cpus} cpu, {net})[/dim]")
        elif mode == "native":
            console.print(f"[green]Sandbox: {native_kind()}[/green] [dim](writes only inside the workspace and "
                          f"temp dirs, {net}; setting: {ctx.sandbox})[/dim]")
        else:
            hint = f" — {why_unavailable()}" if ctx.sandbox == "auto" else ""
            console.print(f"[yellow]Sandbox: off[/yellow] [dim](shell commands run directly on this machine"
                          f"{escape(hint)})[/dim]")

    def cmd_mcp(self, arg):
        from hubble import mcp
        sub, _, rest = arg.partition(" ")
        servers = self.agent.settings.get("mcp_servers") or {}
        if sub in ("login", "auth", "logout"):
            name = rest.strip()
            if name not in servers or not servers[name].get("url"):
                console.print(f"Usage: /mcp {sub} <server>  (a server with a 'url' in mcp_servers)")
                return
            from hubble.mcp_oauth import OAuthError, TokenStore
            store = TokenStore()
            if sub == "logout":
                store.forget(name)
                console.print(f"[green]Logged out of {escape(name)}.[/green]")
                return
            cfg = mcp.config_from(name, servers[name], self.agent.ctx.root)
            www = (mcp.PENDING_LOGIN.get(name) or (None, ""))[1]
            try:
                store.login(cfg, www, notice=lambda m: console.print(f"[dim]{escape(m)}[/dim]"))
            except (OAuthError, KeyboardInterrupt) as e:
                console.print(f"[red]Login failed: {escape(str(e) or 'cancelled')}[/red]")
                return
            self._reconnect_mcp(name)
            return
        if not servers:
            console.print("[dim]No MCP servers configured. Add one to mcp_servers in settings.json "
                          '(local: {"command": [...]}, remote: {"url": "https://..."}).[/dim]')
            return
        clients = {c.config.name: c for c in mcp.mcp_clients(self.agent.tools)}
        for name, entry in servers.items():
            c = clients.get(name)
            kind = "remote" if entry.get("url") else "local"
            if c:
                tools = [t.tool_name for t in self.agent.tools if isinstance(t, mcp.MCPTool) and t.client is c]
                extra = f"; prompts: {', '.join(p['name'] for p in c.prompts)}" if c.prompts else ""
                console.print(f"  [green]{escape(name)}[/green] [dim]({kind})[/dim]  {len(tools)} tool(s): "
                              f"{escape(', '.join(tools))}{escape(extra)}")
            elif name in mcp.PENDING_LOGIN:
                console.print(f"  [yellow]{escape(name)}[/yellow] [dim]({kind})[/dim]  needs login: /mcp login {escape(name)}")
            else:
                console.print(f"  [red]{escape(name)}[/red] [dim]({kind})[/dim]  not connected")

    def _reconnect_mcp(self, name: str):
        from hubble import mcp
        entry = (self.agent.settings.get("mcp_servers") or {}).get(name) or {}
        client = mcp.connect(name, entry, self.agent.ctx.root, self.agent.events)
        if client is None:
            return
        old = [t for t in self.agent.tools if isinstance(t, mcp.MCPTool) and t.client.config.name == name]
        for c in {id(t.client): t.client for t in old}.values():
            c.stop()
        self.agent.tools = [t for t in self.agent.tools if t not in old] + mcp.tools_for(client)
        self._register_mcp_prompts()
        console.print(f"[green]MCP server '{escape(name)}' connected: {len(client.tools)} tool(s).[/green]")

    def _register_mcp_prompts(self):
        """Each MCP prompt becomes /mcp__<server>__<prompt> [args]; args are key=value pairs, or
        plain text for a prompt's first argument."""
        from hubble.mcp import mcp_clients
        for client in mcp_clients(self.agent.tools):
            for p in client.prompts:
                name = f"mcp__{client.config.name}__{p['name']}".lower()
                arg_names = [a.get("name") for a in p.get("arguments") or [] if a.get("name")]

                def run(arg, client=client, prompt=p["name"], arg_names=arg_names):
                    args: Dict[str, str] = {}
                    pairs = re.findall(r'(\w+)=("[^"]*"|\S+)', arg)
                    if pairs:
                        args = {k: v.strip('"') for k, v in pairs}
                    elif arg and arg_names:
                        args = {arg_names[0]: arg}
                    try:
                        text = client.get_prompt(prompt, args)
                    except Exception as e:
                        console.print(f"[red]{escape(str(e))}[/red]")
                        return
                    self.send(text)
                hint = " ".join(f"{a}=..." for a in arg_names) or "[args]"
                self._register(name, run, f"(MCP prompt, {client.config.name}) {p.get('description') or p['name']}",
                               args=hint)

    def cmd_hooks(self, arg):
        hooks = self.agent.settings.get("hooks") or {}
        if not hooks:
            console.print("[dim]No hooks configured. Add them under 'hooks' in settings.json.[/dim]")
            return
        for event, entries in hooks.items():
            for entry in entries:
                matcher = entry.get("matcher") or "*"
                console.print(f"  [bold]{escape(event)}[/bold] [dim]({escape(matcher)})[/dim]  {escape(entry.get('command', ''))}")

    def cmd_temp(self, arg):
        if not arg:
            console.print(f"Temperature: {self.agent.temperature}")
            return
        try:
            val = float(arg)
            if not 0 <= val <= 2:
                raise ValueError
        except ValueError:
            console.print("[red]Temperature must be between 0 and 2[/red]")
            return
        self.agent.temperature = val
    def cmd_subagent(self, arg):
        policy = str(self.agent.settings.get("subagent_policy") or "auto").lower()
        arg = arg.strip().lower()
        if not arg:
            console.print(f"Sub-agent policy is [bold]{policy}[/bold].")
            console.print("  [dim]auto[/dim]      Always available; strict prompt guidelines prevent casual use")
            console.print("  [dim]explicit[/dim]  Only active when your message explicitly mentions agent/subagent")
            console.print("  [dim]off[/dim]       Completely disabled")
            return
        if arg not in ("auto", "explicit", "off"):
            console.print(f"[red]Unknown policy '{arg}'. Use auto, explicit, or off.[/red]")
            return
        self.agent.settings["subagent_policy"] = arg
        save_user_setting("subagent_policy", arg)
        console.print(f"Sub-agent policy set to [bold green]{arg}[/bold green].")

    def cmd_config(self, arg):
        s = self.agent.settings
        for key in ("model", "base_url", "max_tokens", "context_window", "auto_compact_ratio", "max_turns",
                    "shell", "shell_timeout", "additional_dirs", "allow_secret_files"):
            console.print(f"  {key:<20} {escape(str(s.get(key)))}")
        perms = self.agent.permissions
        console.print(f"  {'allow rules':<20} {escape(', '.join(perms.allow + perms.session_allow) or '-')}")
        console.print(f"  {'deny rules':<20} {escape(', '.join(perms.deny) or '-')}")
        console.print(f"[dim]  files: {HOME_DIR / 'settings.json'}, .hubble/settings.json, .hubble/settings.local.json[/dim]")

    def cmd_export(self, arg):
        path = Path(arg or f"hubble_chat_{time.strftime('%Y%m%d_%H%M%S')}.md")
        if not path.is_absolute():
            path = self.agent.ctx.root / path
        lines = [f"# Hubble session {self.agent.session.id if self.agent.session else ''}", "",
                 f"Model: {self.agent.model}", ""]
        for m in self.agent.messages:
            role = m.get("role")
            if role == "tool":
                lines += [f"### Tool result ({m.get('name', '')})", "```", truncate(m.get("content") or "", 3000), "```", ""]
                continue
            lines += [f"### {role.capitalize()}", content_text(m.get("content") or ""), ""]
            for tc in m.get("tool_calls") or []:
                lines += [f"- tool call `{tc['function']['name']}` `{truncate(tc['function']['arguments'], 500)}`"]
        path.write_text("\n".join(lines), encoding="utf-8")
        console.print(f"[green]Exported to {escape(str(path))}[/green]")


def register_provider(cfg: ProviderConfig, ids):
    save_provider(cfg)
    save_listing(cfg.name, cfg.base_url, ids)


def session_items(items):
    out = []
    for it in items:
        when = time.strftime("%m-%d %H:%M", time.localtime(it["updated"]))
        title = (it["title"] or "(no prompt)")[:55]
        out.append((it["id"], f"{when}  {title}", f"{it['messages']} msgs · {it['model']}"))
    return out


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def print_history_tail(messages, n: int = 4):
    shown = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")][-n:]
    for m in shown:
        who = "[bold yellow]you[/bold yellow]" if m["role"] == "user" else "[bold green]hubble[/bold green]"
        text = content_text(m["content"]).strip().splitlines()
        preview = " ".join(text)[:200] if text else ""
        console.print(f"  {who} [dim]{escape(preview)}[/dim]")
