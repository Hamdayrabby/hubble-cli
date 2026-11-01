"""Antigravity CLI runtime adapter (Spark tier).

Verified against ``agy`` 1.4.4 installed at
``C:/Users/SOWDA/AppData/Local/agy/bin/agy.exe`` (not on PATH, so that
directory is always on the fallback search path).

Headless form::

    agy -p <prompt> --output-format stream-json [--mode ...]
        [--model ...] [--effort ...] [--conversation <ID> | --continue]

Contract notes
--------------
* argv arrays only; spawned via ProcRun with ``spec.child_env()``.
* No ``--worktree`` flag exists: run isolation is ``cwd=spec.cwd``.
* ``--dangerously-skip-permissions`` is never passed by this adapter;
  ``permission_profile`` maps to ``--mode`` (default ``accept-edits``;
  ``read-only`` maps to ``plan``; explicit ``accept-edits``/``auto``/
  ``plan`` pass through).
* ``send()`` raises UnsupportedCapability. ``agy --input-format
  stream-json`` does accept interactive stdin turns, but P0 stays one-shot:
  driving a half-duplex stdin-turn protocol was not trivially supportable
  without risking deadlocks, so follow-ups use ``resume()`` instead.
* Extra agy flags (``--sandbox``, ``--add-dir``, ``--print-timeout``,
  ``--json-schema``) are intentionally unmapped; pass them via
  ``RunSpec.extra_args``.
* stream-json lines are mapped defensively: assistant text -> stdout,
  errors -> stderr, token stats -> usage, approval prompts ->
  approval_required, warnings -> warning, everything else -> status with
  bounded ``raw``. Malformed JSONL lines become ``parse_error`` events and
  never stop the stream. No prompt, env, or secret content is put in events.
"""

import itertools
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

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

AGY_NAMES = ["agy"]
AGY_EXTRA_PATHS = [Path("C:/Users/SOWDA/AppData/Local/agy/bin")]
MODES = ("accept-edits", "auto", "plan")
READ_ONLY_PROFILES = {"read-only", "readonly", "read_only"}
CONTINUE_SENTINEL = "continue"
TEXT_KEYS = ("text", "content", "message", "output", "delta", "body",
             "result", "result_text", "response", "reply")
SESSION_KEYS = ("conversation_id", "conversationId", "session_id", "sessionId")
USAGE_KEYS = ("usage", "token_usage", "input_tokens", "output_tokens")
TEXT_LIMIT = 4000


def _mode_for(permission_profile: str) -> str:
    profile = (permission_profile or "").strip()
    if profile in MODES:
        return profile
    if profile.lower().replace("-", "_") in ("read_only",):
        return "plan"
    return "accept-edits"


def _extract_text(node: Any, depth: int = 0) -> str:
    """Best-effort text pull from an unknown stream-json shape."""
    if depth > 4 or node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, list):
        parts = [_extract_text(item, depth + 1) for item in node]
        return "".join(part for part in parts if part)
    if isinstance(node, dict):
        for key in TEXT_KEYS:
            if key in node:
                got = _extract_text(node[key], depth + 1)
                if got:
                    return got
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


def _map_object(obj: Any, seq: EventSequencer, raw: str) -> RuntimeEvent:
    if not isinstance(obj, dict):
        return seq.emit("status", {"state": "unknown"}, raw=raw)
    etype = str(obj.get("type", "") or "").lower()
    if etype in ("error", "failure", "fail"):
        detail = _extract_text(obj) or raw
        return seq.emit("stderr", {"text": detail[:TEXT_LIMIT]})
    if etype in ("usage", "stats", "metrics", "token_usage") or any(
            key in obj for key in USAGE_KEYS):
        return seq.emit("usage", {"usage": _usage_dict(obj)})
    if etype in ("approval_required", "approval", "permission_prompt",
                 "need_approval", "permission_request"):
        summary = _extract_text(obj)[:500]
        return seq.emit("approval_required", {"summary": summary}, raw=raw)
    if etype == "warning":
        detail = _extract_text(obj) or raw
        return seq.emit("warning", {"text": detail[:TEXT_LIMIT]})
    text = _extract_text(obj)
    if text:
        return seq.emit("stdout", {"text": text[:TEXT_LIMIT]})
    return seq.emit("status", {"state": etype or "unknown"}, raw=raw)


class AntigravityAdapter:
    runtime_id = "antigravity"

    def __init__(self, executable: str = "",
                 extra_paths: Optional[List[Path]] = None):
        self._executable_override = executable
        self._extra_paths = list(extra_paths) if extra_paths is not None else list(AGY_EXTRA_PATHS)
        self._counter = itertools.count(1)
        self._specs: Dict[str, RunSpec] = {}

    def _resolve_executable(self) -> str:
        if self._executable_override:
            return self._executable_override
        return find_executable(AGY_NAMES, extra_paths=self._extra_paths)

    def detect(self) -> DetectResult:
        exe = self._resolve_executable()
        if not exe:
            return DetectResult(
                runtime_id=self.runtime_id, available=False,
                diagnostics=["agy executable not found on PATH or in "
                             + ", ".join(str(p) for p in self._extra_paths)])
        version = probe_version(exe, ["--version"])
        return DetectResult(runtime_id=self.runtime_id, available=True,
                            executable=exe, version=version)

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            one_shot=True, interactive=False, json_stream=True,
            resume=True, cancel=True, worktree_flag=False,
            model_override=True, effort_override=True,
            approval_controls=False)

    def build_argv(self, spec: RunSpec, session_ref: str = "") -> List[str]:
        exe = self._resolve_executable()
        if not exe:
            raise RuntimeUnavailable(
                "agy executable not found on PATH or in "
                + ", ".join(str(p) for p in self._extra_paths))
        argv = [exe, "-p", spec.prompt, "--output-format", "stream-json",
                "--mode", _mode_for(spec.permission_profile)]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        if session_ref == CONTINUE_SENTINEL:
            argv.append("--continue")
        elif session_ref:
            argv += ["--conversation", session_ref]
        argv += list(spec.extra_args)
        return argv

    def _spawn(self, spec: RunSpec, argv: List[str]) -> RunHandle:
        try:
            proc = ProcRun(argv, spec.cwd, spec.child_env())
        except OSError as exc:
            raise RuntimeUnavailable(f"agy failed to launch: {exc}") from exc
        run_id = spec.run_id or f"antigravity-{next(self._counter)}"
        self._specs[run_id] = spec
        return RunHandle(runtime_id=self.runtime_id, run_id=run_id,
                         task_id=spec.task_id, pid=proc.pid, proc=proc)

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
        try:
            for line in proc.iter_lines(timeout_s=timeout_s):
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    yield seq.emit("parse_error",
                                         {"error": "malformed JSONL line"},
                                         raw=text)
                    continue
                if isinstance(obj, dict):
                    for key in SESSION_KEYS:
                        value = obj.get(key)
                        if isinstance(value, str) and value and not handle.session_ref:
                            handle.session_ref = value
                            break
                yield _map_object(obj, seq, text)
        except TimeoutError as exc:
            yield seq.emit("failed", {"error": str(exc), "timeout": True})
            return
        if handle.cancelled:
            yield seq.emit("cancelled", {})
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
            "antigravity adapter is one-shot; send is unsupported "
            "(agy --input-format stream-json stdin turns exist, but P0 does "
            "not drive them; use resume with a conversation id instead)")

    def cancel(self, handle: RunHandle) -> str:
        handle.cancelled = True
        proc = handle.proc
        if proc is None:
            return "no process attached; run marked cancelled"
        try:
            return proc.kill_tree()
        except OSError as exc:
            return "cancel requested; kill failed: %s" % (exc,)

    def resume(self, session_ref: str, spec: RunSpec) -> RunHandle:
        if not session_ref:
            raise RuntimeUnavailable("cannot resume without a session_ref")
        handle = self._spawn(spec, self.build_argv(spec, session_ref=session_ref))
        if session_ref != CONTINUE_SENTINEL:
            handle.session_ref = session_ref
        return handle

    def close(self, handle: RunHandle) -> None:
        self._specs.pop(handle.run_id, None)
        return None
