"""OpenCode runtime adapter: headless ``opencode run --format json``.

Verified CLI surface (opencode, npm shim)::

    opencode run [flags] <message...>

Relevant flags: ``--format json`` (choices default|json), ``-m/--model
provider/model#variant``, ``--agent``, ``-s/--session <ID>``,
``-c/--continue``, ``--fork``, ``--auto`` (auto-approve not-denied),
``--thinking``, ``-f/--file``. The top-level also accepts a positional
directory, but this adapter runs with ``cwd=spec.cwd`` instead so the
working tree never depends on flag parsing.

Contract notes:

- argv arrays only, never shell strings; spawn via :class:`ProcRun` with
  ``spec.child_env()`` so only allowlisted variables cross into the child.
- Malformed JSONL lines become ``parse_error`` events, never swallowed.
- Secrets never appear in events: env and prompt text are never copied into
  payloads; harness ``raw`` output is bounded to 2000 chars (and scrubbed).
- ``send()`` raises :class:`UnsupportedCapability` (one-shot runtime).
- Resume starts a NEW ``opencode run`` carrying the prior session via
  ``-s <session>`` (no ``--fork``); pass ``fork=True`` (or ``--fork`` in
  ``extra_args``) to fork instead of continuing in place.
- ``--auto`` is NEVER passed by default: it is added only when
  ``permission_profile`` explicitly requests it (``auto``, ``full``,
  ``permissive``, ``never-ask``, ``yolo``, ``auto-approve``).
  Yolo/bypass-style flags smuggled in via ``extra_args`` are stripped
  defensively. ``spec.effort`` is intentionally unmapped; pass
  ``--thinking``/``--agent``/``-f`` via ``extra_args``.
- JSON output shape is not fully documented, so every line is parsed
  defensively: text-ish fields become ``stdout``, errors become
  ``failed``/``stderr``, usage-ish records become ``usage``, and anything
  else becomes ``status`` with bounded ``raw``. Session ids are harvested
  from ``session``/``session_id`` keys.
"""

import itertools
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from hubble.runtimes.adapters._proc import ProcRun, find_executable, probe_version
from hubble.runtimes.events import EventSequencer, RuntimeEvent
from hubble.runtimes.protocol import (
    DetectResult,
    RunHandle,
    RunSpec,
    RuntimeCapabilities,
    RuntimeUnavailable,
    UnsupportedCapability,
)

EXECUTABLE_NAMES = ["opencode"]
RAW_LIMIT = 2000
TEXT_LIMIT = 4000

# Profiles that explicitly request auto-approve (not-denied). Anything else
# -- including the empty default -- runs without ``--auto``.
_AUTO_PROFILES = {"auto", "full", "permissive", "never-ask", "yolo", "auto-approve"}

# Never let these reach the child even if slipped into ``extra_args``.
FORBIDDEN_FLAGS = (
    "--yolo",
    "--auto-approve",
    "--approve-all",
    "--bypass-permissions",
    "--dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
)

_TEXT_KEYS = ("text", "content", "message", "output", "delta", "body",
              "response", "reply", "result", "result_text")
_TEXT_TYPES = {"text", "message", "content", "output", "output_text", "delta",
               "assistant", "response", "reply", "stdout", "chunk"}
_USAGE_TYPES = {"usage", "token_usage", "tokens", "metrics", "stats"}
_APPROVAL_TYPES = {"approval", "approval_required", "permission_request",
                   "permission", "need_approval"}
_DONE_TYPES = {"result", "final", "done", "complete", "completed", "exit"}
_ERROR_TYPES = {"error", "fatal", "exception", "failure", "failed"}

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s\"']+"),
    re.compile(r"(?i)(secret\s*[:=]\s*)[^\s\"']+"),
    re.compile(r"(?i)(bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(token\s*[:=]\s*)[^\s\"']+"),
)


def _scrub(text: str) -> str:
    for i, pat in enumerate(_SECRET_PATTERNS):
        text = pat.sub(r"\1***" if i else "sk-***", text)
    return text


def wants_auto_approve(profile: str) -> bool:
    """True only when the permission profile explicitly requests ``--auto``."""
    return (profile or "").strip().lower() in _AUTO_PROFILES


def _extract_text(node: Any, depth: int = 0) -> str:
    """Best-effort text pull from an undocumented JSON shape."""
    if depth > 4 or node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, list):
        parts = [_extract_text(item, depth + 1) for item in node]
        return "".join(part for part in parts if part)
    if isinstance(node, dict):
        for key in _TEXT_KEYS:
            if key in node:
                got = _extract_text(node[key], depth + 1)
                if got:
                    return got
    return ""


def _extract_session_id(obj: Dict[str, Any]) -> str:
    for key in ("session_id", "sessionId"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    sess = obj.get("session")
    if isinstance(sess, str) and sess:
        return sess
    if isinstance(sess, dict):
        for key in ("id", "session_id", "sessionId"):
            val = sess.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def _usage_dict(obj: Dict[str, Any]) -> Dict[str, Any]:
    nested = obj.get("usage")
    src = nested if isinstance(nested, dict) else obj
    out: Dict[str, Any] = {}
    for key, value in src.items():
        if key == "type":
            continue
        if isinstance(value, (int, float)):
            out[key] = value
        else:
            out[key] = str(value)[:200]
    return out


def _is_usage(obj: Dict[str, Any], kind: str) -> bool:
    if kind in _USAGE_TYPES:
        return True
    if isinstance(obj.get("usage"), dict):
        return True
    return any(k in obj for k in ("input_tokens", "output_tokens",
                                 "token_usage", "prompt_tokens",
                                 "completion_tokens"))


def _map_record(obj: Dict[str, Any], raw_line: str,
                seq: EventSequencer) -> Tuple[RuntimeEvent, bool]:
    """Map one decoded JSONL record to ``(event, is_terminal)``.

    Unknown shapes stay ``status`` events with bounded ``raw``.
    """
    kind = str(obj.get("type", "") or "").strip().lower()
    text = _extract_text(obj)
    if _is_usage(obj, kind):
        return seq.emit("usage", {"usage": _usage_dict(obj)},
                        raw=raw_line), False
    if kind in _APPROVAL_TYPES:
        return seq.emit("approval_required", {"summary": text[:500]},
                        raw=raw_line), False
    if kind in _DONE_TYPES:
        code_raw = obj.get("exit_code", obj.get("exitCode", 0))
        try:
            code = int(code_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            code = 0 if not obj.get("error") else 1
        err = obj.get("error")
        failed = (code != 0 or bool(err)
                  or obj.get("ok") is False or obj.get("success") is False)
        if failed:
            payload: Dict[str, Any] = {"exit_code": code or 1}
            detail = err if isinstance(err, str) and err else text
            if detail:
                payload["text"] = detail[:500]
            return seq.emit("failed", payload), True
        return seq.emit("completed", {"exit_code": 0}), True
    err = obj.get("error")
    if kind in _ERROR_TYPES or (isinstance(err, str) and err.strip()):
        fatal = obj.get("fatal") is True or (
            kind in _ERROR_TYPES and ("exit_code" in obj or "exitCode" in obj))
        detail = (err if isinstance(err, str) and err.strip()
                  else text or raw_line[:500])
        if fatal:
            payload = {"exit_code": 1}
            if detail:
                payload["text"] = detail[:500]
            return seq.emit("failed", payload), True
        return seq.emit("stderr", {"text": detail[:TEXT_LIMIT]}), False
    if text:
        return seq.emit("stdout", {"text": text[:TEXT_LIMIT]}), False
    return seq.emit("status", {"kind": kind or "unknown"},
                    raw=raw_line), False


class OpenCodeAdapter:
    runtime_id = "opencode"

    def __init__(self, executable: str = "") -> None:
        self._executable = executable
        self._counter = itertools.count(1)
        self._specs: Dict[str, RunSpec] = {}

    def _resolve_executable(self) -> str:
        if self._executable:
            return self._executable
        return find_executable(EXECUTABLE_NAMES)

    def detect(self) -> DetectResult:
        exe = self._resolve_executable()
        if not exe:
            return DetectResult(
                runtime_id=self.runtime_id, available=False,
                diagnostics=["opencode executable not found on PATH"])
        version = probe_version(exe, ["--version"])
        if not version:
            return DetectResult(
                runtime_id=self.runtime_id, available=False, executable=exe,
                diagnostics=[f"found {exe} but --version produced no output"])
        return DetectResult(runtime_id=self.runtime_id, available=True,
                            executable=exe, version=version)

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            one_shot=True, interactive=False, json_stream=True,
            resume=True, cancel=True, worktree_flag=False,
            model_override=True, effort_override=False,
            approval_controls=False)

    def build_argv(self, spec: RunSpec, session_ref: str = "",
                   fork: bool = False) -> List[str]:
        exe = self._resolve_executable()
        if not exe:
            raise RuntimeUnavailable(
                "opencode executable not found on PATH; "
                "install opencode or fix PATH")
        argv = [exe, "run", "--format", "json"]
        if spec.model:
            argv += ["-m", spec.model]
        if session_ref:
            argv += ["-s", session_ref]
        if fork and "--fork" not in spec.extra_args:
            argv.append("--fork")
        if wants_auto_approve(spec.permission_profile):
            argv.append("--auto")
        for arg in spec.extra_args:
            if arg in FORBIDDEN_FLAGS:
                continue
            argv.append(arg)
        if spec.prompt:
            argv.append(spec.prompt)
        return argv

    def _spawn(self, spec: RunSpec, argv: List[str],
               session_ref: str = "") -> RunHandle:
        assert all(isinstance(a, str) for a in argv)
        try:
            proc = ProcRun(argv, cwd=spec.cwd, env=spec.child_env())
        except OSError as exc:
            raise RuntimeUnavailable(
                f"opencode failed to launch: {exc}") from exc
        run_id = spec.run_id or f"opencode-{next(self._counter)}"
        self._specs[run_id] = spec
        return RunHandle(runtime_id=self.runtime_id, run_id=run_id,
                         task_id=spec.task_id, pid=proc.pid,
                         session_ref=session_ref, proc=proc)

    def start(self, spec: RunSpec) -> RunHandle:
        return self._spawn(spec, self.build_argv(spec))

    def stream(self, handle: RunHandle) -> Iterator[RuntimeEvent]:
        seq = EventSequencer(task_id=handle.task_id, run_id=handle.run_id,
                             runtime_id=self.runtime_id)
        proc = handle.proc
        spec = self._specs.get(handle.run_id)
        timeout_s = spec.timeout_s if spec is not None else 600.0
        try:
            pid = proc.pid if proc is not None else 0
        except OSError:
            pid = 0
        yield seq.emit("process_started", {"pid": pid})
        if proc is None:
            yield seq.emit("failed", {"exit_code": 1,
                                      "error": "no process attached to handle"})
            return
        terminal = False
        try:
            for line in proc.iter_lines(timeout_s=timeout_s):
                if handle.cancelled:
                    yield seq.emit("cancelled", {})
                    return
                stripped = _scrub(line.strip())
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except ValueError:
                    yield seq.emit("parse_error",
                                   {"error": "malformed JSONL line"},
                                   raw=stripped[:RAW_LIMIT])
                    continue
                if not isinstance(obj, dict):
                    yield seq.emit("status", {"kind": "unknown"},
                                   raw=stripped[:RAW_LIMIT])
                    continue
                sid = _extract_session_id(obj)
                if sid and not handle.session_ref:
                    handle.session_ref = sid
                event, terminal = _map_record(obj, stripped[:RAW_LIMIT], seq)
                yield event
        except TimeoutError as exc:
            yield seq.emit("failed", {"error": str(exc), "timeout": True})
            return
        if handle.cancelled:
            yield seq.emit("cancelled", {})
            return
        if terminal:
            return
        try:
            rc = proc.wait()
        except OSError as exc:
            yield seq.emit("failed", {"exit_code": 1,
                                      "error": "wait failed: %s" % (exc,)})
            return
        code = rc if isinstance(rc, int) else 1
        if code == 0:
            yield seq.emit("completed", {"exit_code": 0})
        else:
            yield seq.emit("failed", {"exit_code": code})

    def send(self, handle: RunHandle, message: str) -> None:
        raise UnsupportedCapability(
            "opencode runtime is one-shot; send is unsupported "
            "(use resume with a session id instead)")

    def cancel(self, handle: RunHandle) -> str:
        handle.cancelled = True
        proc = handle.proc
        if proc is None:
            return "no process attached; run marked cancelled"
        try:
            return proc.kill_tree()
        except OSError as exc:
            return "cancel requested; kill failed: %s" % (exc,)

    def resume(self, session_ref: str, spec: RunSpec,
               fork: bool = False) -> RunHandle:
        if not session_ref:
            raise RuntimeUnavailable("cannot resume without a session_ref")
        handle = self._spawn(
            spec, self.build_argv(spec, session_ref=session_ref, fork=fork),
            session_ref=session_ref)
        return handle

    def close(self, handle: RunHandle) -> None:
        self._specs.pop(handle.run_id, None)
        return None
