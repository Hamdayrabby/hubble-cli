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
        self.turn_stats = None
        self.agent = agent
        agent.fallback_resolver = self.fallback_for
        self.store = store
        self.last_model_list: List[str] = []
        self.commands: Dict[str, Command] = {}
        self._register_builtin()
        self._load_custom_commands()

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
          ("providers",), "[add|remove <name>|list]")
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
        r("sandbox", self.cmd_sandbox, "Run shell commands in an isolated Docker container instead of "
          "directly on this machine", args="[on|off]")
        r("config", self.cmd_config, "Show effective settings and allow/deny rules")
        r("export", self.cmd_export, "Export conversation to markdown", ("copy",), "[file]")
        r("skills", self.cmd_skills, "List skills (the model can also call these on its own)")
        r("skill", self.cmd_skill_new, "Create a new skill template to fill in", args="new <name> [user]")

    def _load_custom_commands(self):
        """Skills become /<name>; a skill's own description is shown in /help and /<tab>."""
        for skill in self.agent.skills:
            self._register(skill.name, self._make_skill_runner(skill),
                           f"(skill, {skill.scope}) {skill.description}", args=skill.args_hint or "[args]")
        # Back-compat: a plain command with no frontmatter, e.g. from an older .hubble/commands/ setup.
        for folder in (HOME_DIR / "commands", self.agent.ctx.root / ".hubble" / "commands"):
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
        return PromptSession(history=FileHistory(str(HOME_DIR / "history")), completer=ReplCompleter(self),
                             complete_while_typing=True, key_bindings=kb, bottom_toolbar=self._toolbar,
                             multiline=False, enable_history_search=False, refresh_interval=1.0,
                             reserve_space_for_menu=14)

    def _toolbar(self):
        a = self.agent
        mode = a.permissions.mode
        color = {"default": "ansigray", "accept-edits": "ansigreen", "plan": "ansiblue", "yolo": "ansired"}[mode]
        ctx_pct = f"{a.context_ratio():.0%}"
        pinned = f" | {len(a.pinned)} pinned" if a.pinned else ""
        prov = f"{escape_html(a.provider_name)}: " if len(self.providers) > 1 else ""
        line1 = (f" {prov}<b>{escape_html(a.model)}</b> | mode: <style fg='{color}'><b>{mode}</b></style>"
                 f" (shift+tab) | persona: {a.persona} | context {ctx_pct}{pinned}"
                 + "".join(f" | {escape_html(n)}: {s.summary()}" for n, s in self.scanners.items() if s.running)
                 + " ")
        return HTML(line1 + "\n" + self._stats_line())

    def _stats_line(self) -> str:
        a = self.agent
        s = self.turn_stats
        session_total = a.total_prompt_tokens + a.total_completion_tokens
        if s is None or not s.model_calls:
            return f" Tokens: no turns yet  •  session total {session_total:,} "
        total = s.prompt_tokens + s.completion_tokens
        speed = s.completion_tokens / s.duration if s.duration else 0.0
        tools = f" | 🔧 {s.tool_calls} tool call{'s' if s.tool_calls != 1 else ''}" if s.tool_calls else ""
        return (f" Tokens: <b>📥 In: {s.prompt_tokens:,}</b> | <b>📤 Out: {s.completion_tokens:,}</b>"
                f" | <b>📊 Total: {total:,}</b>  •  Time: {s.duration:.2f}s | 🚀 {speed:.1f} tok/s{tools}"
                f"  •  session {session_total:,} ")

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
        # Leave room for the prompt line and the 2-line toolbar, plus the completion menu while typing / or @.
        budget = size.rows - 4 - (10 if typed[:1] in ("/", "@") or " @" in typed else 0)
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
            cfg = self.providers[name]
            self.clients[name] = OpenAICompatProvider(cfg.base_url, cfg.api_key)
        return self.clients[name]

    def start_scan(self, name: str, reason: str) -> bool:
        cfg = self.providers.get(name)
        if not cfg:
            return False
        scanner = self.scanners.get(name)
        if scanner is None:
            scanner = self.scanners[name] = ModelScanner(cfg.base_url, cfg.api_key, output=scan_file(name))
        if not scanner.start():
            return False
        self._unannounced.add(name)
        console.print(f"[dim]  {reason} Checking which {escape(name)} models are available in the background "
                      "(progress in the bottom bar)...[/dim]")
        return True

    def _announce_scans(self):
        for name in list(self._unannounced):
            scanner = self.scanners.get(name)
            if not scanner or scanner.running:
                continue
            self._unannounced.discard(name)
            if scanner.status == "done":
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
            if not cfg.check_models:
                continue
            age = scan_age_hours(scan_file(name))
            if age is None or hours <= 0 or age >= hours:
                reason = "Model list is missing." if age is None else (
                    "Checking on startup." if hours <= 0 else f"Model list is {age:.0f}h old.")
                self.start_scan(name, reason)

    def run(self, initial_prompt: Optional[str] = None):
        self._home = None
        self.banner()
        self._refresh_stale_scans()
        session = self._prompt_session()
        if initial_prompt:
            self._home = None
            self.handle(initial_prompt)
        while True:
            try:
                self._announce_scans()
                if self._home is not None:
                    # Animated home screen lives in the prompt until the first message is sent;
                    # the last frame stays in the scrollback.
                    text = session.prompt(self._home_message, refresh_interval=1 / 30, reserve_space_for_menu=0)
                    self._home = None
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
        prompt = self.expand_mentions(text)
        self.agent.run(prompt)
        if self.agent.last_stats.model_calls:
            self.turn_stats = self.agent.last_stats  # shown in the bar under the input box

    def expand_mentions(self, text: str) -> str:
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
                      "  @path          attach a file or directory listing to your message\n"
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
        try:
            save_user_setting("model", name)
            save_user_setting("provider", provider)
            saved = " (saved as default)"
        except OSError:
            saved = ""
        prov = f"{escape(provider)}: " if len(self.providers) > 1 else ""
        console.print(f"[green]Model: {prov}{escape(name)}{saved}[/green]")

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
        if cfg.check_models:
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
        ctx = self.agent.ctx
        arg = arg.lower().strip()
        if arg in ("on", "docker"):
            if not shutil.which("docker"):
                console.print("[red]docker was not found on PATH. Install Docker Desktop first.[/red]")
                return
            ctx.sandbox = "docker"
        elif arg == "off":
            ctx.sandbox = "off"
        elif arg:
            console.print("Usage: /sandbox [on|off]")
            return
        if arg:
            self.agent.settings["shell_sandbox"] = ctx.sandbox
            save_user_setting("shell_sandbox", ctx.sandbox)
        if ctx.sandbox == "docker":
            net = "with network access" if ctx.sandbox_network else "network disabled"
            console.print(f"[green]Sandbox: docker[/green] [dim](image {ctx.sandbox_image}, "
                          f"{ctx.sandbox_memory} mem, {ctx.sandbox_cpus} cpu, {net})[/dim]")
        else:
            console.print("[yellow]Sandbox: off[/yellow] [dim](shell commands run directly on this machine)[/dim]")

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
            lines += [f"### {role.capitalize()}", m.get("content") or "", ""]
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
        text = m["content"].strip().splitlines()
        preview = " ".join(text)[:200] if text else ""
        console.print(f"  {who} [dim]{escape(preview)}[/dim]")
