"""Hooks: user-configured shell commands that run at fixed points in the agent loop.

Modeled on Claude Code's hook events. Each hook receives a JSON payload on stdin describing
what is about to happen (or just happened) and can:
  - block it, by exiting non-zero (stderr becomes the reason shown to the model) or by
    printing {"decision": "block", "reason": "..."} as its only stdout,
  - or let it through while adding context, by printing {"additionalContext": "..."},
  - or just observe (log, notify, lint) and print nothing meaningful.

Hooks are configuration, not model output, so they are checked into `.hubble/settings.json`
like `shell_sandbox` and require the same folder-trust before an untrusted clone can install one.
"""

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")


@dataclass
class HookResult:
    decision: str = "allow"   # "allow" | "block"
    reason: str = ""
    additional_context: str = ""

    def __bool__(self):  # convenience: `if result:` means "allowed"
        return self.decision != "block"


def _matches(matcher: Optional[str], name: Optional[str]) -> bool:
    if not matcher or matcher == "*":
        return True
    if name is None:
        return False
    import fnmatch
    return fnmatch.fnmatchcase(name, matcher) or name == matcher


class HookRunner:
    def __init__(self, hooks_config: Dict[str, List[Dict[str, Any]]], root, shell_argv: List[str],
                 events=None):
        self.config = hooks_config or {}
        self.root = root
        self.shell_argv = shell_argv
        self.events = events  # optional UI events object, for surfacing hook stdout/errors

    def _notice(self, message: str, level: str = "dim"):
        if self.events is not None:
            self.events.notice(message, level)

    def run(self, event: str, payload: Dict[str, Any], name: Optional[str] = None) -> HookResult:
        """Run every hook registered for `event` whose matcher matches `name`. First block wins;
        additional_context from every hook that returns one is concatenated."""
        entries = self.config.get(event) or []
        if not entries:
            return HookResult()
        contexts = []
        for entry in entries:
            if not _matches(entry.get("matcher"), name):
                continue
            result = self._run_one(event, entry, payload)
            if result.decision == "block":
                return result
            if result.additional_context:
                contexts.append(result.additional_context)
        return HookResult(additional_context="\n".join(contexts))

    def _run_one(self, event: str, entry: Dict[str, Any], payload: Dict[str, Any]) -> HookResult:
        command = entry.get("command")
        if not command:
            return HookResult()
        timeout = min(max(1, int(entry.get("timeout", 30))), 120)
        body = json.dumps({"event": event, **payload}, ensure_ascii=False, default=str)
        try:
            proc = subprocess.run(self.shell_argv + [command], cwd=self.root, input=body,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            self._notice(f"hook for {event} timed out after {timeout}s: {command}", "warn")
            return HookResult()
        except OSError as e:
            self._notice(f"hook for {event} could not run: {e}", "warn")
            return HookResult()

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            reason = stderr or stdout or f"hook exited {proc.returncode}"
            return HookResult(decision="block", reason=reason)
        if stdout:
            try:
                data = json.loads(stdout)
            except ValueError:
                data = None
            if isinstance(data, dict):
                decision = data.get("decision", "allow")
                if decision == "block":
                    return HookResult(decision="block", reason=data.get("reason", "blocked by hook"))
                return HookResult(additional_context=str(data.get("additionalContext", "")))
            # Non-JSON stdout on success: treat as a log line, not a directive.
            self._notice(f"[{event} hook] {stdout[:300]}")
        return HookResult()
