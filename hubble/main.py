"""`hubble` entry point: interactive REPL, headless -p mode, session resume."""

import argparse
import json
import os
import sys
from pathlib import Path

from hubble import __version__
from hubble.permissions import MODES, Permissions
from hubble.prompts import PERSONAS
from hubble.provider import OpenAICompatProvider
from hubble.session import SessionStore
from hubble.settings import ConfigError, load_settings, require_api_key, trust_folder
from hubble.tools import ToolContext, detect_shell


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hubble", description="Hubble: agentic coding CLI for AIHub and other OpenAI-compatible APIs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  hubble                                  start an interactive session
  hubble "explain this repo"              start a session with a first prompt
  hubble -p "fix the failing test" --permission-mode accept-edits
  git diff | hubble -p "review this diff" --output-format json
  hubble -c                               continue the latest session here
  hubble -r                               pick a session to resume
""")
    p.add_argument("prompt", nargs="?", help="initial prompt")
    p.add_argument("-p", "--print", dest="print_mode", action="store_true",
                   help="non-interactive: run the prompt, print the result and exit")
    p.add_argument("-c", "--continue", dest="continue_", action="store_true", help="continue the latest session")
    p.add_argument("-r", "--resume", nargs="?", const="", metavar="ID", help="resume a session (lists them without ID)")
    p.add_argument("-m", "--model", help="model id")
    p.add_argument("--provider", help="provider name from /provider (default: last used)")
    p.add_argument("--permission-mode", choices=MODES, help="default | accept-edits | plan | yolo")
    p.add_argument("--yolo", action="store_true", help="shortcut for --permission-mode yolo (no approvals)")
    p.add_argument("--persona", choices=list(PERSONAS), help="system persona")
    p.add_argument("--output-format", choices=["text", "json", "stream-json"], default="text",
                   help="output format for -p (stream-json: one JSON event per line as it happens)")
    p.add_argument("--github", action="store_true",
                   help="with -p, inside GitHub Actions: build the prompt from the PR/issue event and post "
                        "the answer as a comment (see /install-github)")
    p.add_argument("--image", action="append", default=[], metavar="FILE",
                   help="with -p: attach an image to the prompt (repeatable; needs a vision model)")
    p.add_argument("--max-turns", type=int, help="max model calls per prompt")
    p.add_argument("--cwd", help="workspace root (default: current directory)")
    p.add_argument("-w", "--worktree", metavar="NAME",
                   help="work in an isolated git worktree (.hubble/worktrees/NAME, branch hubble/NAME); "
                        "created if missing")
    p.add_argument("--base-url", help="API base URL")
    p.add_argument("--api-key", help="API key (default: HUBBLE_API_KEY / .env)")
    p.add_argument("--allow", action="append", default=[], metavar="RULE", help='allow rule, e.g. "shell(pytest*)"')
    p.add_argument("--deny", action="append", default=[], metavar="RULE", help='deny rule, e.g. "shell(git push*)"')
    p.add_argument("--no-session", action="store_true", help="do not save this session")
    p.add_argument("--quiet", action="store_true", help="with -p: hide tool progress on stderr")
    p.add_argument("--test", metavar="MODEL", help="check that a model responds, then exit")
    p.add_argument("-v", "--version", action="version", version=f"hubble {__version__}")
    return p


def main(argv=None):
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")  # model output often contains emoji
            except (AttributeError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    root = Path(args.cwd or ".").resolve()
    if not root.is_dir():
        sys.exit(f"hubble: workspace not found: {root}")
    if args.worktree:
        from hubble import worktree
        try:
            existing = next((w for w in worktree.list_worktrees(root) if w["name"] == args.worktree), None)
            if existing:
                root = Path(existing["path"])
            else:
                info = worktree.create(root, args.worktree)
                root = Path(info["path"])
                print(f"hubble: created worktree {info['path']} on branch {info['branch']}", file=sys.stderr)
        except worktree.WorktreeError as e:
            sys.exit(f"hubble: {e}")

    overrides = {
        "model": args.model, "base_url": args.base_url, "api_key": args.api_key,
        "persona": args.persona, "max_turns": args.max_turns,
        "permission_mode": "yolo" if args.yolo else args.permission_mode,
    }
    interactive = not args.print_mode and sys.stdin.isatty()
    try:
        settings = load_settings(root, overrides)
        ignored = settings["_ignored_project_keys"]
        if ignored:
            if _ask_trust(root, ignored, interactive=interactive):
                trust_folder(root)
                settings = load_settings(root, overrides)
    except ConfigError as e:
        sys.exit(f"hubble: {e}")

    from hubble.providers import DEFAULT_PROVIDER, load_providers
    providers = load_providers(settings)
    active = args.provider or settings.get("provider") or DEFAULT_PROVIDER
    if args.api_key or args.base_url:
        active = DEFAULT_PROVIDER  # explicit credentials on the command line win
    if active not in providers:
        if args.provider:
            sys.exit(f"hubble: unknown provider '{args.provider}'. Known: {', '.join(providers) or 'none'}")
        if providers:
            active = DEFAULT_PROVIDER if DEFAULT_PROVIDER in providers else next(iter(providers))
        elif interactive:
            active = _first_run(providers, settings)
        else:
            try:
                require_api_key(settings)
            except ConfigError as e:
                sys.exit(f"hubble: {e}")
    cfg = providers[active]
    settings["provider"] = active
    provider = OpenAICompatProvider(cfg.base_url, cfg.api_key)

    if args.test:
        res = provider.ping(args.test)
        print(f"{'OK' if res['ok'] else 'FAILED'} {args.test} {res['latency_ms']} ms  {res['msg']}")
        sys.exit(0 if res["ok"] else 1)

    ctx = ToolContext(
        root=root,
        extra_dirs=[Path(d).expanduser().resolve() for d in settings.get("additional_dirs", [])],
        allow_secrets=bool(settings.get("allow_secret_files")),
        shell_argv=detect_shell(settings.get("shell", "auto")),
        shell_timeout=int(settings.get("shell_timeout", 120)),
        sandbox=settings.get("shell_sandbox", "auto"),
        sandbox_writable=[Path(d).expanduser().resolve() for d in settings.get("sandbox_writable", [])],
        sandbox_image=settings.get("sandbox_image", "python:3.12-slim"),
        sandbox_memory=settings.get("sandbox_memory", "1g"),
        sandbox_cpus=str(settings.get("sandbox_cpus", "2")),
        sandbox_network=bool(settings.get("sandbox_network", True)),
    )
    perms = Permissions(settings["permission_mode"],
                        allow=settings["permissions"].get("allow", []) + args.allow,
                        deny=settings["permissions"].get("deny", []) + args.deny)
    store = SessionStore(root)

    prompt = args.prompt or ""
    if args.resume and not any(args.resume in it["id"] for it in store.list(limit=500)):
        # `hubble -r "fix the bug"`: argparse took the prompt as a session id.
        prompt = f"{args.resume} {prompt}".strip()
        args.resume = ""
    if args.print_mode and not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            prompt = f"{prompt}\n\n<stdin>\n{piped}\n</stdin>" if prompt else piped

    if args.print_mode:
        args._providers = providers
        sys.exit(run_headless(args, settings, provider, ctx, perms, store, prompt,
                              _fallback_client(providers, active, provider)))

    from hubble.agent import Agent
    try:
        from hubble.repl import Repl
        from hubble.ui import ReplEvents, console
    except ImportError as e:
        sys.exit(f"hubble: missing dependency ({e.name}). Install with:\n"
                 f'  "{sys.executable}" -m pip install -r "{Path(__file__).resolve().parent.parent / "requirements.txt"}"')

    agent = Agent(provider, settings, ctx, perms, ReplEvents(ctx, show_reasoning=bool(settings.get("show_reasoning"))))
    agent.fallback_client = _fallback_client(providers, active, provider)
    repl = Repl(agent, store, providers)
    resumed = _resume(args, store, agent, console)
    if resumed is None:
        return
    if not resumed and not args.no_session:
        agent.session = store.new(agent.model)
    repl.run(initial_prompt=prompt or None)


def _headless_resolver(settings, providers, agent):
    from hubble.providers import resolve_fallback
    clients = {agent.provider_name: agent.provider}

    def resolve(provider_name, model):
        choice = resolve_fallback(settings, providers, provider_name, model)
        if not choice:
            return None
        name, fb = choice
        if name not in clients:
            if name not in providers:
                return None
            clients[name] = OpenAICompatProvider(providers[name].base_url, providers[name].api_key)
        return clients[name], fb
    return resolve


def _fallback_client(providers, active, current):
    """fallback_model lives on the built-in hubble provider."""
    from hubble.providers import DEFAULT_PROVIDER
    if active == DEFAULT_PROVIDER:
        return current
    cfg = providers.get(DEFAULT_PROVIDER)
    return OpenAICompatProvider(cfg.base_url, cfg.api_key) if cfg else None


def _first_run(providers, settings) -> str:
    from hubble.onboarding import add_provider_wizard
    from hubble.repl import register_provider
    from hubble.settings import save_user_setting
    from hubble.ui import console, pick

    result = add_provider_wizard(providers.keys(), first_run=True)
    if not result:
        sys.exit("hubble: no provider configured.")
    cfg, ids = result
    register_provider(cfg, ids)
    providers[cfg.name] = cfg
    model = pick(f"Choose a model ({len(ids)} listed)", [(m, m, "") for m in sorted(ids)]) or sorted(ids)[0]
    settings["model"] = model
    save_user_setting("provider", cfg.name)
    save_user_setting("model", model)
    console.print(f"[green]Saved. Using {cfg.name}: {model}[/green]")
    return cfg.name


def _ask_trust(root: Path, ignored, interactive: bool) -> bool:
    never = [k for k in ignored if k in ("base_url", "api_key")]
    if never:
        print(f"hubble: ignoring {', '.join(never)} from {root / '.hubble'} (only user settings or env may set them)",
              file=sys.stderr)
    rest = [k for k in ignored if k not in ("base_url", "api_key")]
    if not rest:
        return False
    if not interactive:
        print(f"hubble: ignoring untrusted project settings: {', '.join(rest)}. Run hubble interactively once "
              "in this folder to trust it.", file=sys.stderr)
        return False
    print(f"This folder's .hubble settings want to change security options: {', '.join(rest)}.")
    try:
        answer = input(f"Trust {root} and apply them? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def _resume(args, store, agent, console):
    """True if a session was loaded, False to start fresh, None to exit."""
    if args.resume == "" and not args.continue_:
        from hubble.repl import session_items
        from hubble.ui import pick
        items = store.list()
        if not items:
            console.print("[dim]No sessions to resume; starting a new one.[/dim]")
            return False
        sid = pick("Resume a session (Esc for a new one)", session_items(items))
        if not sid:
            return False
    elif args.resume or args.continue_:
        sid = args.resume or store.latest()
        if not sid:
            console.print("[dim]No previous session here; starting a new one.[/dim]")
            return False
    else:
        return False
    try:
        session, messages, meta = store.load(sid)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        return None
    agent.session = session
    agent.load_history(messages)
    # Keep the model only if the session used the provider that is active now.
    from hubble.providers import normalize_provider_name
    if meta.get("model") and not args.model and normalize_provider_name(meta.get("provider")) == agent.provider_name:
        agent.model = meta["model"]
    console.print(f"[green]Resumed session {session.id}[/green] [dim]({len(messages)} messages)[/dim]")
    from hubble.repl import print_history_tail
    print_history_tail(messages)
    return True


def run_headless(args, settings, provider, ctx, perms, store, prompt, fallback_client=None) -> int:
    from hubble.agent import Agent
    from hubble.ui import HeadlessEvents, StreamJsonEvents

    gh_task = None
    if getattr(args, "github", False):
        from hubble import github
        try:
            gh_task = github.build_task(*github.load_event())
        except (github.GitHubError, ValueError) as e:
            print(f"hubble: {e}", file=sys.stderr)
            return 2
        if gh_task is None:
            print("hubble: nothing to do for this GitHub event (no @hubble mention, or not a PR/issue event)",
                  file=sys.stderr)
            return 0
        prompt = gh_task["prompt"] + (f"\n\n{prompt}" if prompt.strip() else "")
        if not gh_task["can_edit"]:
            perms.deny += ["write_file", "edit_file"]  # reviews and issue answers never change files

    if not prompt.strip():
        print("hubble: -p needs a prompt (argument or stdin)", file=sys.stderr)
        return 2
    streaming = args.output_format == "stream-json"
    events = StreamJsonEvents() if streaming else HeadlessEvents(stream_text=args.output_format == "text",
                                                                 quiet=args.quiet)
    agent = Agent(provider, settings, ctx, perms, events)
    agent.fallback_client = fallback_client
    agent.fallback_resolver = _headless_resolver(settings, getattr(args, "_providers", {}), agent)
    if args.continue_ or args.resume:
        sid = args.resume or store.latest()
        if sid:
            try:
                session, messages, _ = store.load(sid)
                agent.session = session
                agent.load_history(messages)
            except FileNotFoundError as e:
                print(f"hubble: {e}", file=sys.stderr)
                return 2
    if agent.session is None and not args.no_session:
        agent.session = store.new(agent.model)

    if streaming:
        events.emit({"type": "init", "session_id": agent.session.id if agent.session else None,
                     "model": agent.model, "provider": agent.provider_name, "cwd": str(ctx.root),
                     "permission_mode": perms.mode, "tools": [t.name for t in agent.tools]})
    images = []
    for f in getattr(args, "image", None) or []:
        from hubble.images import ImageError, data_url
        try:
            images.append(data_url(Path(f).expanduser().resolve()))
        except (ImageError, OSError) as e:
            print(f"hubble: --image {f}: {e}", file=sys.stderr)
            return 2
    agent.start_session("resume" if agent.messages else "startup")
    result = agent.run(prompt, images or None)
    agent.shutdown()
    stats = agent.last_stats
    is_error = bool(stats.error) or stats.interrupted
    summary = {
        "result": result, "is_error": is_error, "error": stats.error,
        "session_id": agent.session.id if agent.session else None, "model": agent.model,
        "num_model_calls": stats.model_calls, "num_tool_calls": stats.tool_calls,
        "usage": {"prompt_tokens": stats.prompt_tokens, "completion_tokens": stats.completion_tokens},
        "duration_s": round(stats.duration, 2),
    }
    if streaming:
        events.emit({"type": "result", **summary})
    elif args.output_format == "json":
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    if gh_task is not None:
        from hubble import github
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        try:
            url = github.post_comment(repo, gh_task["number"], github.format_reply(result, stats, agent.model, is_error))
            print(f"hubble: posted {url}", file=sys.stderr)
        except (github.GitHubError, OSError) as e:
            print(f"hubble: {e}", file=sys.stderr)
            return 1
    return 1 if is_error else 0


if __name__ == "__main__":
    main()
