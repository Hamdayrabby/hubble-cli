"""Interactive REPL: prompt_toolkit input, slash commands, @file mentions, # memory notes."""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import completion_is_selected
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
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
from hubble.ui import console, pick, render_todos

MENTION_RX = re.compile(r"(?<![\w@])@([\w./\\:-]+)")
MAX_MENTION_CHARS = 60000


class Command:
    def __init__(self, name: str, fn: Callable, help: str, aliases: Tuple[str, ...] = (), args: str = ""):
        self.name, self.fn, self.help, self.aliases, self.args = name, fn, help, aliases, args


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
            base_dir = self.repl.agent.ctx.root / os.path.dirname(partial)
            prefix = os.path.basename(partial)
            try:
                entries = sorted(base_dir.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
            except OSError:
                return
            for e in entries[:200]:
                if e.name.startswith(prefix) and e.name not in (".git", "__pycache__", "node_modules"):
                    rel = (Path(os.path.dirname(partial)) / e.name).as_posix().removeprefix("./")
                    yield Completion(rel + ("/" if e.is_dir() else ""), start_position=-len(partial))


class Repl:
    def __init__(self, agent: Agent, store: SessionStore, providers: Optional[Dict[str, ProviderConfig]] = None):
        self.providers: Dict[str, ProviderConfig] = dict(providers or {})
        self.clients: Dict[str, OpenAICompatProvider] = {agent.provider_name: agent.provider}
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
        self.turn_stats = None
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
        r("persona", self.cmd_persona, f"System persona: {', '.join(PERSONAS)}", args="[name]")
        r("resume", self.cmd_resume, "Resume a previous session of this project", ("sessions",), "[id|#]")
        r("cost", self.cmd_cost, "Token usage and context size", ("stats", "tokens", "status"))
        r("context", self.cmd_context, "Show what is in the context window")
        r("undo", self.cmd_undo, "Revert file changes from the last turn that edited files", ("rewind",))
        r("diff", self.cmd_diff, "Show git diff of the workspace")
        r("init", self.cmd_init, "Generate HUBBLE.md project instructions")
        r("memory", self.cmd_memory, "Show loaded memory files (HUBBLE.md / AGENTS.md)")
        r("add", self.cmd_add, "Pin a file into the system prompt", ("pin",), "<file>")
        r("drop", self.cmd_drop, "Unpin a file", ("unpin",), "<file>")
        r("files", self.cmd_files, "List pinned files")
        r("todos", self.cmd_todos, "Show the current task list")
        r("test", self.cmd_test, "Check a model responds and measure latency", args="[model]")
        r("temp", self.cmd_temp, "Show or set temperature", args="[0-2]")
        r("fallback", self.cmd_fallback, "Choose the model used when the current one is rate limited",
          args="[model|auto|off]")
        r("thinking", self.cmd_thinking, "Show or hide the model's reasoning text", args="[on|off]")
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
        r("agents", self.cmd_agents, "List custom sub-agents, or create one", ("agent",), "[new <name> [user]]")
        r("plugin", self.cmd_plugin, "List, install, remove, enable or disable plugins", ("plugins",),
          "[list|install <src>|remove <name>]")

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

        @kb.add("escape", "enter")
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add("escape", "v")  # Alt+V; terminals keep Ctrl+V for their own text paste
        def _(event):
            placeholder = self.paste_image()
            if placeholder:
                event.current_buffer.insert_text(placeholder + " ")
            else:
                from prompt_toolkit.application import run_in_terminal
                run_in_terminal(lambda: console.print("[dim]No image on the clipboard.[/dim]"))

        @kb.add("c-j")
        def _(event):
            event.current_buffer.insert_text("\n")

        @kb.add("s-tab")
        def _(event):
            self.agent.permissions.cycle_mode()
            event.app.invalidate()

        @kb.add("enter", filter=completion_is_selected)
        def _(event):
            # A highlighted item in the dropdown (e.g. /model <space>, @file): one Enter both
            # picks it and submits, instead of submitting the still-unfinished typed text.
            buf = event.current_buffer
            buf.apply_completion(buf.complete_state.current_completion)
            buf.validate_and_handle()

        HOME_DIR.mkdir(parents=True, exist_ok=True)
        from prompt_toolkit.styles import Style
        return PromptSession(history=FileHistory(str(HOME_DIR / "history")), completer=ReplCompleter(self),
                             complete_while_typing=True, key_bindings=kb, bottom_toolbar=self._toolbar,
                             multiline=False, enable_history_search=False, refresh_interval=1.0,
                             reserve_space_for_menu=14, style=Style.from_dict(self.TOOLBAR_STYLE))

    SEP = "<tb.sep>  ·  </tb.sep>"

    def _toolbar(self):
        a = self.agent
        mode = a.permissions.mode
        ratio = a.context_ratio()
        ctx_cls = "tb.warn" if ratio >= 0.8 else "tb.val"
        prov = f"<tb.dim>{escape_html(a.provider_name)} </tb.dim>" if len(self.providers) > 1 else ""
        parts = [f"{prov}<tb.model>{escape_html(a.model)}</tb.model>",
                 f"<tb.mode-{mode}>{mode}</tb.mode-{mode}> <tb.dim>shift+tab</tb.dim>",
                 f"<tb.dim>persona</tb.dim> <tb.val>{a.persona}</tb.val>",
                 f"<tb.dim>context</tb.dim> <{ctx_cls}>{ratio:.0%}</{ctx_cls}>"]
        if a.pinned:
            parts.append(f"<tb.val>{len(a.pinned)}</tb.val> <tb.dim>pinned</tb.dim>")
        for n, s in self.scanners.items():
            if s.running:
                parts.append(f"<tb.dim>checking {escape_html(n)}</tb.dim> <tb.val>{s.progress()}</tb.val>")
        self._sync_scan_visuals()
        try:
            from prompt_toolkit.application import get_app
            width = get_app().output.get_size().columns
        except Exception:
            width = 80
        rule = f"<tb.rule>{'─' * max(0, width - 1)}</tb.rule>"
        return HTML(rule + "\n  " + self.SEP.join(parts) + "\n  " + self._stats_line())

    TOOLBAR_STYLE = {
        # No reverse-video bar: the toolbar sits on the terminal's own background.
        "bottom-toolbar": "noreverse bg:default #6c6c80",
        "bottom-toolbar.text": "noreverse bg:default",
        "tb.rule": "#303040", "tb.sep": "#44445a", "tb.dim": "#6c6c80", "tb.val": "#b8b8cc",
        "tb.model": "bold #00d7ff", "tb.warn": "bold #ffaf00",
        "tb.mode-default": "#b8b8cc", "tb.mode-accept-edits": "bold #5fd787",
        "tb.mode-plan": "bold #5fafff", "tb.mode-yolo": "bold #ff5f5f",
        "tb.in": "#87afd7", "tb.out": "#af87d7", "tb.speed": "#5fd7af",
    }

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
        return dict(version=__version__, provider=a.provider_name, model=a.model, mode=a.permissions.mode,
                    root=str(a.ctx.root), session_id=a.session.id if a.session else None,
                    memory_files=[p.name for p, _ in a.memory], model_count=len(known) or None,
                    provider_count=len(self.providers), resumable=max(0, len(self.store.list()) - 1),
                    show_provider=len(self.providers) > 1)

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
            from hubble.banner import render_home
            render_home(console, **self._home_info())

    def _home_message(self):
        """Prompt message for the first prompt: the whole home screen, re-rendered every frame."""
        from prompt_toolkit.application import get_app
        from prompt_toolkit.formatted_text import FormattedText
        from hubble.banner import compose_home_fragments
        try:
            app = get_app()
            size = app.output.get_size()
            typed = app.current_buffer.text
        except Exception:
            return HTML("<ansicyan><b>❯</b></ansicyan> ")
        # Leave room for the prompt line and the 3-line toolbar, plus the completion menu while typing / or @.
        budget = size.rows - 5 - (10 if typed[:1] in ("/", "@") or " @" in typed else 0)
        # prompt_toolkit asks for the message several times per redraw (measure, then draw);
        # build each frame once per 1/30 s tick and reuse it.
        elapsed = time.time() - self._home_t0
        key = (int(elapsed * 30), size.columns, budget)
        cached = getattr(self, "_home_frame", None)
        if cached and cached[0] == key:
            return cached[1]
        status, tips = self._home
        frags = compose_home_fragments(size.columns, max(budget, 3), elapsed, status, tips, __version__)
        frame = FormattedText(frags + [("bold fg:ansicyan", "❯"), ("", " ")])
        self._home_frame = (key, frame)
        return frame

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

    def run(self, initial_prompt: Optional[str] = None):
        self._home = None
        self.agent.start_session("resume" if self.agent.messages else "startup")
        self.banner()
        self._refresh_stale_scans()
        self.updates.start()
        session = self._prompt_session()
        if initial_prompt:
            self._home = None
            self.handle(initial_prompt)
        while True:
            try:
                self._announce_scans()
                self._announce_update()
                if self._home is not None:
                    # Animated home screen lives in the prompt until the first message is sent;
                    # the last frame stays in the scrollback.
                    text = session.prompt(self._home_message, refresh_interval=1 / 30, reserve_space_for_menu=0)
                    self._home = None
                    # PromptSession.prompt() kwargs overwrite the session permanently, not just for
                    # that one call -- without resetting these, every later prompt keeps 0 reserved
                    # menu space (so "/" never shows a dropdown again) and a needlessly fast redraw.
                    session.reserve_space_for_menu = 14
                    session.refresh_interval = 1.0
                else:
                    text = session.prompt(HTML("<ansicyan><b>❯</b></ansicyan> "))
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            try:
                if self.handle(text) is False:
                    break
            except KeyboardInterrupt:
                console.print("[yellow]Interrupted.[/yellow]")
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
        self.send(text)
        return None

    def send(self, text: str):
        images: List[str] = []
        prompt = self.expand_mentions(text, images)
        prompt = self._attach_pasted_images(prompt, images)
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

    def _attach_pasted_images(self, prompt: str, images: List[str]) -> str:
        for i, path in enumerate(self._pending_images, 1):
            if f"[Image #{i}]" not in prompt:
                continue  # the placeholder was deleted before sending: drop that image
            try:
                images.append(data_url(path))
                console.print(f"[dim]  attached pasted image #{i}[/dim]")
            except (ImageError, OSError) as e:
                console.print(f"[yellow]Skipped image #{i}: {escape(str(e))}[/yellow]")
        for path in self._pending_images:
            path.unlink(missing_ok=True)
        self._pending_images = []
        return prompt

    def expand_mentions(self, text: str, images: Optional[List[str]] = None) -> str:
        attachments = []
        for m in MENTION_RX.finditer(text):
            raw = m.group(1).rstrip(".,:;")
            try:
                p = self.agent.ctx.resolve(raw)
            except Exception:
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
                      "  @path          attach a file or directory listing (or an image) to your message\n"
                      "  Alt+V          paste an image from the clipboard (needs a vision model)\n"
                      "  !command       run a shell command yourself (not sent to the model)\n"
                      "  #note          append a note to ./HUBBLE.md (project memory)\n"
                      "  Esc+Enter      new line (also Ctrl+J)\n"
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
        console.print(f"[green]Model: {prov}{escape(name)}{saved}[/green]")

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
        console.print(f"  model          {escape(a.model)}")
        console.print(f"  input tokens   {a.total_prompt_tokens:,}")
        console.print(f"  output tokens  {a.total_completion_tokens:,}")
        console.print(f"  context        ~{a.context_tokens:,} / {window:,} ({a.context_ratio():.0%})")
        console.print(f"  messages       {len(a.messages)}")

    def cmd_context(self, arg):
        a = self.agent
        from hubble.agent import estimate_messages, estimate_tokens
        sys_tokens = estimate_tokens(a.system_prompt())
        tool_tokens = estimate_tokens(str([t.schema() for t in a.tools]))
        console.print(f"  system prompt  ~{sys_tokens:,} tokens "
                      f"(memory: {len(a.memory)} file(s), pinned: {len(a.pinned)})")
        console.print(f"  tool schemas   ~{tool_tokens:,} tokens ({len(a.tools)} tools)")
        console.print(f"  messages       ~{estimate_messages(a.messages):,} tokens ({len(a.messages)} messages)")

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

    def cmd_skills(self, arg):
        if not self.agent.skills:
            console.print("[dim]No skills yet. Ask the model to save one, or /skill new <name>.[/dim]")
            return
        for s in self.agent.skills:
            console.print(f"  [bold]/{s.name}[/bold] [dim]({s.scope})[/dim]  {escape(s.description)}")
        console.print("[dim]Run one with /<name>, or the model calls them on its own when relevant.[/dim]")

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
        from hubble.subagents import TEMPLATE as AGENT_TEMPLATE
        parts = arg.split()
        if parts and parts[0] == "new":
            if len(parts) < 2 or not re.match(r"^[a-z0-9][a-z0-9_-]{0,40}$", parts[1].lower()):
                console.print("Usage: /agents new <name> [user]  (lowercase letters, digits, - or _)")
                return
            name = parts[1].lower()
            base = HOME_DIR / "agents" if len(parts) > 2 and parts[2] == "user" else \
                self.agent.ctx.root / ".hubble" / "agents"
            path = base / f"{name}.md"
            if path.exists():
                console.print(f"[yellow]Already exists: {escape(str(path))}[/yellow]")
                return
            base.mkdir(parents=True, exist_ok=True)
            path.write_text(AGENT_TEMPLATE.format(name=name), encoding="utf-8")
            self.agent.reload_agents()
            console.print(f"[green]Created {escape(str(path))}[/green] [dim]— write its description and prompt; "
                          "the model delegates to it with the task tool.[/dim]")
            return
        if not self.agent.agent_defs:
            console.print("[dim]No custom agents. /agents new <name> creates one in .hubble/agents/.[/dim]")
            return
        for a in self.agent.agent_defs:
            extra = ", ".join(x for x in (a.capability, a.model and f"model {a.model}",
                                          a.tools and f"tools: {', '.join(a.tools)}") if x)
            console.print(f"  [bold]{escape(a.name)}[/bold] [dim]({escape(a.scope)}; {escape(extra)})[/dim]  "
                          f"{escape(a.description)}")
        console.print("[dim]The model picks one with the task tool; or ask: \"use the <name> agent to ...\"[/dim]")

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

    def cmd_thinking(self, arg):
        ev = self.agent.events
        if arg.lower() in ("on", "off"):
            ev.show_reasoning = arg.lower() == "on"
        else:
            ev.show_reasoning = not getattr(ev, "show_reasoning", False)
        state = "shown" if ev.show_reasoning else "hidden (the spinner counts thinking tokens)"
        save_user_setting("show_reasoning", ev.show_reasoning)
        console.print(f"[green]Reasoning text {state}.[/green]")

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
        console.print(f"[green]Temperature: {val}[/green]")

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
        who = "[bold cyan]you[/bold cyan]" if m["role"] == "user" else "[bold magenta]hubble[/bold magenta]"
        text = content_text(m["content"]).strip().splitlines()
        preview = " ".join(text)[:200] if text else ""
        console.print(f"  {who} [dim]{escape(preview)}[/dim]")
