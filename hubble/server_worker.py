"""Hubble as a server-side worker: one agent session driven over JSON lines on stdin/stdout.

This is the building block for a hosted (web) Hubble: an orchestrator starts one worker per
session, forwards what the browser sends to the worker's stdin and streams the worker's stdout
back to the browser (e.g. over SSE). The agent loop, tools and approvals are exactly the CLI's.

    python -m hubble.server_worker --workspace /workspace

Environment (set by the orchestrator, never by the user's repo):
    HUBBLE_WORKER_BASE_URL   provider base URL (validated: https + public address)
    HUBBLE_WORKER_API_KEY    the user's key for that provider (passed per session, never stored)
    HUBBLE_WORKER_KIND       "openai" (default) or "anthropic"
    HUBBLE_WORKER_MODEL      model id
    HUBBLE_WORKER_SANDBOX    shell sandbox: "docker" (default) | "native" | "off" (tests/dev only)
    HUBBLE_HOME              per-user/session state dir (sessions, stats), keeps tenants apart

In (one JSON object per line):
    {"type": "prompt", "text": "...", "images": ["data:image/png;base64,..."]}
    {"type": "answer", "id": 3, "answer": "yes" | "always" | "no", "feedback": "..."}
    {"type": "cancel"}          stop the current turn (like Ctrl+C)
    {"type": "shutdown"}

Out (one JSON object per line): "ready", every agent event (text, reasoning, assistant,
tool_use, tool_result, todos, notice, turn_start, subagent_*, batch_*), "ask" (the agent is
waiting for an approval: id, tool, kind, target, preview, always), "result" after each prompt,
and "error" for malformed input.

Server-safe by construction: the user's repository settings are never trusted, and hooks, MCP
servers, plugin hooks, yolo mode, extra directories, secret files and `unsandboxed` shell calls
are all switched off regardless of what any settings file says.
"""

import argparse
import ipaddress
import json
import os
import queue
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO
from urllib.parse import urlparse

from hubble.agent import Agent, describe_call
from hubble.permissions import Permissions
from hubble.tools import ToolContext, detect_shell
from hubble.ui import StreamJsonEvents, _command_family

SAFE_MODES = ("default", "accept-edits", "plan")


class WorkerError(Exception):
    pass


# ----- safety ------------------------------------------------------------------

def check_base_url(url: str, allow_private: bool = False) -> str:
    """Refuse provider URLs that would let a user reach the server's own network (SSRF)."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise WorkerError(f"invalid provider URL: {url!r}")
    if allow_private:
        return url
    if parsed.scheme != "https":
        raise WorkerError("provider URL must use https")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise WorkerError(f"cannot resolve provider host {parsed.hostname}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            raise WorkerError(f"provider host {parsed.hostname} resolves to a non-public address ({ip})")
    return url


def safe_settings(settings: Dict[str, Any], sandbox: str) -> Dict[str, Any]:
    """Settings with every server-unsafe option forced off, whatever the files said."""
    s = dict(settings)
    s.update({
        "hooks": {}, "mcp_servers": {},            # no user-supplied commands on the server
        "additional_dirs": [], "allow_secret_files": False,
        "shell_sandbox": sandbox,
        "update_check": False, "model_refresh_hours": None,
        "home_animation": False,
    })
    if s.get("permission_mode") not in SAFE_MODES:
        s["permission_mode"] = "default"          # never yolo on a server
    return s


# ----- events ------------------------------------------------------------------

class WebEvents(StreamJsonEvents):
    """Every agent event as a JSON line, plus a real, blocking approval round-trip."""

    def __init__(self, write: Callable[[str], None], answers: "queue.Queue[Dict[str, Any]]",
                 cancel: threading.Event):
        super().__init__(include_deltas=True)
        self._write = write
        self._lock = threading.Lock()
        self._answers = answers
        self._cancel = cancel
        self._ask_id = 0

    def emit(self, obj: Dict[str, Any]):  # instance method: our own stream, thread-safe
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with self._lock:
            self._write(line + "\n")

    # Events StreamJsonEvents does not forward, which a web UI needs.
    def turn_start(self):
        self.emit({"type": "turn_start"})

    def reasoning(self, delta):
        if delta:
            self.emit({"type": "reasoning", "delta": delta})

    def tool_result(self, tool, args, output, is_error):
        self.emit({"type": "tool_result", "tool": tool.name, "is_error": is_error, "output": output[:200_000],
                   "truncated": len(output) > 200_000})

    def batch_start(self, count):
        self.emit({"type": "batch_start", "count": count})

    def batch_end(self):
        self.emit({"type": "batch_end"})

    def subagent_step(self, key, action, is_tool=True):
        self.emit({"type": "subagent_step", "id": key, "action": action, "is_tool": is_tool})

    def subagent_tokens(self, key, tokens):
        self.emit({"type": "subagent_tokens", "id": key, "tokens": tokens})

    def ask(self, tool, args, preview):
        self._ask_id += 1
        ask_id = self._ask_id
        always = {"edit": "edits", "web": tool.target(args), "mcp": tool.target(args)}.get(
            tool.kind, _command_family(args.get("command", "")))
        self.emit({"type": "ask", "id": ask_id, "tool": tool.name, "kind": tool.kind,
                   "target": describe_call(tool, args), "args": args, "preview": preview or "",
                   "always": always})
        while True:
            if self._cancel.is_set():
                raise KeyboardInterrupt
            try:
                msg = self._answers.get(timeout=0.2)
            except queue.Empty:
                continue
            if msg.get("id") != ask_id:
                continue  # a stale answer to an earlier question
            answer = msg.get("answer")
            if answer not in ("yes", "always", "no"):
                answer = "no"
            return answer, str(msg.get("feedback") or "")


# ----- the worker -----------------------------------------------------------------

class Worker:
    """Reads commands, runs prompts one at a time, answers approvals, supports cancel."""

    def __init__(self, agent: Agent, inp: TextIO, write: Callable[[str], None],
                 answers: "queue.Queue[Dict[str, Any]]", cancel: threading.Event):
        self.agent = agent
        self.inp = inp
        self.write = write
        self.answers = answers
        self.cancel = cancel
        self.prompts: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()

    def _reader(self):
        """Route input: answers/cancel act at once (even mid-turn); prompts queue up."""
        for line in self.inp:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.agent.events.emit({"type": "error", "message": "input is not JSON"})
                continue
            kind = msg.get("type") if isinstance(msg, dict) else None
            if kind == "answer":
                self.answers.put(msg)
            elif kind == "cancel":
                self.cancel.set()
                self.agent.cancel.set()
            elif kind == "prompt":
                self.prompts.put(msg)
            elif kind == "shutdown":
                break
            else:
                self.agent.events.emit({"type": "error", "message": f"unknown message type {kind!r}"})
        self.prompts.put(None)

    def serve(self):
        threading.Thread(target=self._reader, daemon=True, name="hubble-worker-input").start()
        ev = self.agent.events
        ev.emit({"type": "ready", "model": self.agent.model, "provider": self.agent.provider_name,
                 "cwd": str(self.agent.ctx.root), "permission_mode": self.agent.permissions.mode,
                 "tools": [t.name for t in self.agent.tools]})
        self.agent.start_session("startup")
        try:
            while True:
                msg = self.prompts.get()
                if msg is None:
                    break
                self.cancel.clear()
                text = str(msg.get("text") or "")
                images = [str(u) for u in (msg.get("images") or []) if str(u).startswith("data:image/")]
                result = self.agent.run(text, images or None)
                s = self.agent.last_stats
                ev.emit({"type": "result", "result": result, "is_error": bool(s.error) or s.interrupted,
                         "error": s.error, "interrupted": s.interrupted, "model": self.agent.model,
                         "num_model_calls": s.model_calls, "num_tool_calls": s.tool_calls,
                         "usage": {"prompt_tokens": s.prompt_tokens, "completion_tokens": s.completion_tokens},
                         "session_id": self.agent.session.id if self.agent.session else None})
        finally:
            self.agent.shutdown()


def build_worker(workspace: Path, inp: TextIO = sys.stdin, out: TextIO = sys.stdout,
                 provider=None, env: Optional[Dict[str, str]] = None) -> Worker:
    """Wire an Agent the way `run_headless` does, with server-safe settings and WebEvents."""
    from hubble.settings import load_settings
    env = dict(os.environ if env is None else env)
    root = workspace.resolve()
    if not root.is_dir():
        raise WorkerError(f"workspace not found: {root}")
    sandbox = env.get("HUBBLE_WORKER_SANDBOX", "docker")
    settings = safe_settings(load_settings(root, trusted=False), sandbox)  # the repo is never trusted
    if env.get("HUBBLE_WORKER_MODEL"):
        settings["model"] = env["HUBBLE_WORKER_MODEL"]
    if provider is None:
        from hubble.providers import ProviderConfig, make_client
        url = check_base_url(env.get("HUBBLE_WORKER_BASE_URL", ""),
                             allow_private=env.get("HUBBLE_WORKER_ALLOW_PRIVATE") == "1")
        key = env.get("HUBBLE_WORKER_API_KEY", "")
        if not key:
            raise WorkerError("HUBBLE_WORKER_API_KEY is not set")
        kind = env.get("HUBBLE_WORKER_KIND", "openai")
        provider = make_client(ProviderConfig("session", url, key, False, kind))
    settings["provider"] = getattr(provider, "hubble_name", None) or "session"

    def write(s: str):
        out.write(s)
        out.flush()

    answers: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    cancel = threading.Event()
    ctx = ToolContext(root=root, shell_argv=detect_shell("bash") if os.name != "nt" else detect_shell(),
                      shell_timeout=int(settings.get("shell_timeout", 120)), sandbox=sandbox,
                      sandbox_image=settings.get("sandbox_image", "python:3.12-slim"),
                      sandbox_memory=settings.get("sandbox_memory", "1g"),
                      sandbox_cpus=str(settings.get("sandbox_cpus", "2")),
                      sandbox_network=bool(env.get("HUBBLE_WORKER_SANDBOX_NETWORK") == "1"),
                      allow_unsandboxed=False)
    perms = Permissions(settings["permission_mode"], deny=settings.get("permissions", {}).get("deny", []))
    events = WebEvents(write, answers, cancel)
    agent = Agent(provider, settings, ctx, perms, events)
    from hubble.session import SessionStore
    agent.session = SessionStore(root).new(agent.model)
    return Worker(agent, inp, write, answers, cancel)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m hubble.server_worker",
                                description="Run one Hubble agent session over JSON lines on stdin/stdout.")
    p.add_argument("--workspace", default="/workspace", help="the only directory the agent may touch")
    args = p.parse_args(argv)
    if sys.platform == "win32":
        for stream in (sys.stdin, sys.stdout):
            try:
                stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError):
                pass
    try:
        worker = build_worker(Path(args.workspace))
    except WorkerError as e:
        sys.stdout.write(json.dumps({"type": "error", "message": str(e), "fatal": True}) + "\n")
        return 2
    worker.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
