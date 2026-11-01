"""Codex runtime adapter: headless ``codex exec`` harnessed as a runtime.

Verified against codex-cli 0.166.0. The executable is usually NOT on PATH;
it lives under the standalone install directory, so detection probes that
location via :func:`find_executable` ``extra_paths``.

Headless form: ``codex exec --json -C <DIR> -s <MODE> [-m MODEL] [ARGS] PROMPT``.
Resume form: ``codex exec --json resume <SESSION_ID> --last ...``.
The ``--dangerously-bypass-approvals-and-sandbox`` flag is NEVER used, even
if a caller slips it into ``extra_args`` (it is stripped defensively).
"""

import itertools
import json
import os
import re
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

SANDBOX_MODES = ["read-only", "workspace-write", "danger-full-access"]

# Verified install location for codex-cli 0.166.0 on the reference machine.
# The executable is NOT on PATH, so this extra search path is required.
VERIFIED_EXE_DIR = Path(r"C:\Users\SOWDA\.codex\packages\standalone\current\bin")

FORBIDDEN_FLAGS = ("--dangerously-bypass-approvals-and-sandbox",)

_RAW_LIMIT = 2000
_TEXT_LIMIT = 32000

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


def _sandbox_for_profile(profile: str) -> str:
    name = (profile or "").strip().lower()
    if name in ("read-only", "readonly"):
        return "read-only"
    if name in ("danger-full-access", "danger", "full-access"):
        return "danger-full-access"
    return "workspace-write"


def _extra_exe_dirs() -> List[Path]:
    dirs: List[Path] = [VERIFIED_EXE_DIR]
    for var in ("USERPROFILE", "HOME"):
        home = os.environ.get(var)
        if not home:
            continue
        cand = Path(home) / ".codex" / "packages" / "standalone" / "current" / "bin"
        if cand not in dirs:
            dirs.append(cand)
    return dirs


def _first_str(obj: Dict[str, Any], keys: tuple = ("error", "message", "text")) -> str:
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


_META_KEYS = ("type", "thread_id", "session_id", "id")
_TEXT_KEYS = ("text", "message", "content", "body", "output", "item",
              "data", "payload", "delta")


def _collect_text(node: Any, depth: int, out: List[str]) -> None:
    if depth < 0 or len(out) >= 20:
        return
    if isinstance(node, str):
        if node.strip():
            out.append(node)
    elif isinstance(node, list):
        for item in node:
            _collect_text(item, depth - 1, out)
    elif isinstance(node, dict):
        for key, val in node.items():
            if key in _META_KEYS or key not in _TEXT_KEYS:
                continue
            _collect_text(val, depth - 1, out)


def _extract_text(obj: Dict[str, Any]) -> str:
    found: List[str] = []
    _collect_text(obj, 6, found)
    text = "\n".join(found)
    if len(text) > _TEXT_LIMIT:
        text = text[:_TEXT_LIMIT]
    return text


class CodexAdapter:
    runtime_id = "codex"

    def __init__(self) -> None:
        self._counter = itertools.count(1)
        self._specs: Dict[str, RunSpec] = {}

    def _executable(self) -> str:
        return find_executable(["codex"], extra_paths=_extra_exe_dirs())

    def detect(self) -> DetectResult:
        exe = self._executable()
        if not exe:
            return DetectResult(
                runtime_id=self.runtime_id,
                available=False,
                diagnostics=["codex executable not found on PATH or in "
                             + str(VERIFIED_EXE_DIR)],
            )
        version = probe_version(exe, ["--version"])
        notes: List[str] = []
        if not version:
            notes.append("codex found but --version probe returned nothing")
        return DetectResult(runtime_id=self.runtime_id, available=True,
                            executable=exe, version=version, diagnostics=notes)

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            one_shot=True,
            interactive=False,
            json_stream=True,
            resume=True,
            cancel=True,
            worktree_flag=True,
            model_override=True,
            effort_override=False,
            approval_controls=False,
            sandbox_modes=list(SANDBOX_MODES),
        )

    def _argv(self, exe: str, spec: RunSpec,
              sub: Optional[List[str]] = None) -> List[str]:
        sandbox = _sandbox_for_profile(spec.permission_profile)
        argv = [exe, "exec", "--json"]
        if sub:
            argv.extend(sub)
        argv.extend(["-C", str(spec.cwd), "-s", sandbox])
        if spec.model:
            argv.extend(["-m", spec.model])
        for arg in spec.extra_args:
            if arg in FORBIDDEN_FLAGS:
                continue
            argv.append(arg)
        if spec.prompt:
            argv.append(spec.prompt)
        return argv

    def start(self, spec: RunSpec) -> RunHandle:
        exe = self._executable()
        if not exe:
            raise RuntimeUnavailable("codex executable not found; "
                                     "install codex-cli or fix PATH")
        argv = self._argv(exe, spec)
        proc = ProcRun(argv, cwd=spec.cwd, env=spec.child_env())
        run_id = spec.run_id or f"codex-{next(self._counter)}"
        self._specs[run_id] = spec
        return RunHandle(runtime_id=self.runtime_id, run_id=run_id,
                         task_id=spec.task_id, pid=proc.pid, proc=proc)

    def resume(self, session_ref: str, spec: RunSpec) -> RunHandle:
        if not session_ref:
            raise ValueError("codex resume needs a non-empty session_ref")
        exe = self._executable()
        if not exe:
            raise RuntimeUnavailable("codex executable not found; "
                                     "install codex-cli or fix PATH")
        argv = self._argv(exe, spec, sub=["resume", session_ref, "--last"])
        proc = ProcRun(argv, cwd=spec.cwd, env=spec.child_env())
        run_id = spec.run_id or f"codex-{next(self._counter)}"
        self._specs[run_id] = spec
        return RunHandle(runtime_id=self.runtime_id, run_id=run_id,
                         task_id=spec.task_id, pid=proc.pid,
                         session_ref=session_ref, proc=proc)

    def _event_for_object(self, obj: Dict[str, Any], raw: str,
                          seq: EventSequencer, handle: RunHandle) -> RuntimeEvent:
        kind = str(obj.get("type", "") or "")
        low = kind.lower()
        if (low in ("error", "turn.failed", "turn_failed")
                or low.endswith((".failed", "_failed", "-failed"))):
            msg = _first_str(obj) or kind or "codex run failed"
            return seq.emit("failed", {"error": _scrub(msg)}, raw=raw)
        if "usage" in low:
            usage = obj.get("usage", obj)
            if not isinstance(usage, dict):
                usage = {"value": usage}
            safe = {k: (_scrub(str(v)) if isinstance(v, str) else v)
                    for k, v in usage.items()}
            return seq.emit("usage", safe)
        if low == "thread.started":
            for key in ("thread_id", "session_id", "id"):
                val = obj.get(key)
                if isinstance(val, str) and val and not handle.session_ref:
                    handle.session_ref = val
                    break
            state: Dict[str, Any] = {"state": kind or "thread.started"}
            if handle.session_ref:
                state["session_id"] = handle.session_ref
            return seq.emit("status", state, raw=raw)
        text = _extract_text(obj)
        contentish = ("message" in low or "item" in low or "output" in low
                      or "text" in low or "response" in low
                      or "assistant" in low or "completed" in low
                      or kind == "")
        if text and contentish:
            return seq.emit("stdout", {"text": _scrub(text)})
        return seq.emit("status", {"state": kind or "event"}, raw=raw)

    def stream(self, handle: RunHandle) -> Iterator[RuntimeEvent]:
        seq = EventSequencer(task_id=handle.task_id, run_id=handle.run_id,
                             runtime_id=self.runtime_id)
        yield seq.emit("process_started", {"pid": handle.pid})
        proc = handle.proc
        if proc is None:
            yield seq.emit("failed", {"error": "codex run has no process"})
            return
        if handle.cancelled:
            yield seq.emit("cancelled", {})
            return
        spec = self._specs.get(handle.run_id)
        timeout = spec.timeout_s if spec is not None else 600.0
        try:
            for line in proc.iter_lines(timeout_s=timeout):
                if handle.cancelled:
                    yield seq.emit("cancelled", {})
                    return
                text = _scrub(line.strip())
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except ValueError:
                    yield seq.emit("parse_error",
                                   {"error": "malformed JSONL line"},
                                   raw=text)
                    continue
                if not isinstance(obj, dict):
                    yield seq.emit("parse_error",
                                   {"error": "expected a JSON object"},
                                   raw=text)
                    continue
                yield self._event_for_object(obj, text, seq, handle)
        except TimeoutError as exc:
            try:
                proc.kill_tree()
            except Exception:
                pass
            yield seq.emit("failed", {"error": str(exc)})
            return
        if handle.cancelled:
            yield seq.emit("cancelled", {})
            return
        try:
            rc = proc.wait()
        except Exception:
            rc = None
        code = 0 if rc is None else rc
        if code == 0:
            yield seq.emit("completed", {"exit_code": 0})
        else:
            yield seq.emit("failed", {"exit_code": code})

    def send(self, handle: RunHandle, message: str) -> None:
        raise UnsupportedCapability("codex runtime is one-shot; send is unsupported")

    def cancel(self, handle: RunHandle) -> str:
        handle.cancelled = True
        proc = handle.proc
        if proc is None:
            return "codex run cancelled (no process)"
        try:
            detail = proc.kill_tree()
        except Exception as exc:
            detail = f"kill failed: {exc}"
        return f"codex run cancelled ({detail})"

    def close(self, handle: RunHandle) -> None:
        self._specs.pop(handle.run_id, None)
        proc = handle.proc
        if proc is not None:
            try:
                if proc.wait(timeout_s=0.0) is None:
                    proc.kill_tree()
            except Exception:
                pass
        return None
