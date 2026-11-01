"""MuseCodeAdapter: run ``muse exec --json`` as a one-shot JSONL harness.

Headless shape (verified against muse 1.4.14)::

    muse exec --json --workspace <PATH> --approval-mode <mode>
        [--model <id>] [--reasoning-effort <level>] [--session-id <UUID>]
        --prompt-file <FILE> [extra_args...]

Contract notes:

- argv arrays only, never shell strings; spawn via :class:`ProcRun` with
  ``spec.child_env()`` so only allowlisted variables cross into the child.
- Malformed JSONL lines become ``parse_error`` events, never swallowed.
- Secrets never appear in events: env and prompt text are never copied into
  payloads; harness ``raw`` output is bounded to 2000 chars.
- ``send()`` raises :class:`UnsupportedCapability` (one-shot runtime).
- Resume choice: ``resume()`` starts a NEW ``exec`` run that carries the
  prior session ref via ``--session-id`` (the CLI continues server-side
  state under a fresh run id) instead of shelling to the interactive
  ``muse resume <session-ref>`` command.
- Permission profiles map to ``--approval-mode``; the default (empty or
  unknown profile) is ``on-request``. This adapter never passes ``--yolo``:
  permissive profiles map to ``--approval-mode never``.
"""

import json
import os
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

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

EXECUTABLE_NAMES = ["muse"]
RAW_LIMIT = 2000

_READONLY_PROFILES = {"readonly", "read-only", "safe", "untrusted", "review"}
_PERMISSIVE_PROFILES = {"full", "auto", "never-ask", "yolo", "permissive"}

_TEXT_TYPES = {"stdout", "text", "output_text", "message", "assistant", "content", "delta"}
_USAGE_TYPES = {"usage", "token_usage", "tokens", "metrics", "stats"}
_APPROVAL_TYPES = {"approval", "approval_required", "permission_request", "permission"}
_DONE_TYPES = {"result", "final", "done", "complete", "completed", "exit"}
_ERROR_TYPES = {"error", "fatal", "exception", "failure"}
_PROGRESS_TYPES = {
    "status", "progress", "step", "tool_call", "toolcall",
    "tool_result", "toolresult", "thinking",
}


def _extra_search_dirs() -> List[Path]:
    dirs: List[Path] = []
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        dirs.append(Path(local_app) / "Programs" / "muse")
    dirs.append(Path.home() / "AppData" / "Local" / "Programs" / "muse")
    return dirs


def approval_mode_for(profile: str) -> str:
    """Map a permission profile to a ``--approval-mode`` value."""
    name = (profile or "").strip().lower()
    if name in _READONLY_PROFILES:
        return "untrusted"
    if name in _PERMISSIVE_PROFILES:
        # Never emit --yolo; "never" ask is the closest non-yolo grant.
        return "never"
    return "on-request"


def _looks_like_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _extract_session_id(obj: Dict[str, Any]) -> str:
    for key in ("session_id", "sessionId"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    sess = obj.get("session")
    if isinstance(sess, str) and sess:
        return sess
    if isinstance(sess, dict):
        for key in ("id", "session_id"):
            val = sess.get(key)
            if isinstance(val, str) and val:
                return val
    return ""


def _extract_text(obj: Dict[str, Any]) -> str:
    for key in ("text", "content", "message", "output"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _map_record(obj: Dict[str, Any], raw_line: str,
                seq: EventSequencer) -> Tuple[RuntimeEvent, bool]:
    """Map one decoded JSONL record to ``(event, is_terminal)``.

    Unknown ``type`` values stay ``status`` events with bounded ``raw``.
    """
    kind = str(obj.get("type", "") or "").strip().lower()
    text = _extract_text(obj)
    if kind in _TEXT_TYPES and text:
        return seq.emit("stdout", {"text": text}), False
    if kind == "stderr":
        return seq.emit("stderr", {"text": text or raw_line[:500]}), False
    if kind in _USAGE_TYPES:
        scalars = {k: v for k, v in obj.items() if isinstance(v, (int, float))}
        return seq.emit("usage", {"usage": scalars}, raw=raw_line), False
    if kind in _APPROVAL_TYPES:
        return seq.emit("approval_required", {"text": text}, raw=raw_line), False
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
    if kind in _ERROR_TYPES:
        if obj.get("fatal") is True or "exit_code" in obj or "exitCode" in obj:
            payload = {"exit_code": 1}
            if text:
                payload["text"] = text[:500]
            return seq.emit("failed", payload), True
        return seq.emit("stderr", {"text": text or raw_line[:500]}), False
    if kind in _PROGRESS_TYPES:
        payload = {"kind": kind}
        if text:
            payload["text"] = text[:500]
        return seq.emit("status", payload, raw=raw_line), False
    return seq.emit("status", {"kind": kind or "unknown"}, raw=raw_line), False


class MuseCodeAdapter:
    runtime_id = "muse-code"

    def __init__(self, executable: str = "") -> None:
        self._executable = executable
        self._prompt_files: Dict[str, str] = {}
        self._timeouts: Dict[str, float] = {}

    def detect(self) -> DetectResult:
        exe = self._executable or find_executable(
            EXECUTABLE_NAMES, extra_paths=_extra_search_dirs())
        if not exe:
            return DetectResult(
                runtime_id=self.runtime_id, available=False,
                diagnostics=["muse CLI not found on PATH or under "
                             "%LOCALAPPDATA%\\Programs\\muse"])
        version = probe_version(exe)
        if not version:
            return DetectResult(
                runtime_id=self.runtime_id, available=False, executable=exe,
                diagnostics=[f"found {exe} but --version produced no output"])
        return DetectResult(runtime_id=self.runtime_id, available=True,
                            executable=exe, version=version)

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            one_shot=True, interactive=False, json_stream=True, resume=True,
            cancel=True, worktree_flag=True, model_override=True,
            effort_override=True,
        )

    @staticmethod
    def _prompt_dir(spec: RunSpec) -> Path:
        # The prompt file lives next to the run (cwd/.hubble/prompts): the
        # only directory the adapter is guaranteed to own and write. HOME
        # and system-temp locations proved unreliable (denied or blocking
        # file creation in sandboxed runs; degenerate tempdirs pointing at
        # the checkout). close() removes the file; the coordinator calls it.
        prompt_dir = Path(spec.cwd) / ".hubble" / "prompts"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        return prompt_dir

    def _write_prompt_file(self, spec: RunSpec) -> str:
        # Never the process cwd: tempfile falls back to cwd on some Windows
        # environments, which leaked prompt files into the user's checkout.
        prompt_dir = self._prompt_dir(spec)
        fd_handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".md", dir=str(prompt_dir),
            prefix="muse-code-prompt-", delete=False)
        try:
            fd_handle.write(spec.prompt)
        finally:
            fd_handle.close()
        return fd_handle.name

    def _start_with_session(self, spec: RunSpec, session_id: str) -> RunHandle:
        exe = self._executable or find_executable(
            EXECUTABLE_NAMES, extra_paths=_extra_search_dirs())
        if not exe:
            raise RuntimeUnavailable(
                "muse CLI not found on PATH or under "
                "%LOCALAPPDATA%\\Programs\\muse")
        prompt_path = self._write_prompt_file(spec)
        argv = [exe, "exec", "--json",
                "--workspace", str(spec.cwd),
                "--approval-mode", approval_mode_for(spec.permission_profile)]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.effort:
            argv += ["--reasoning-effort", spec.effort]
        if session_id:
            argv += ["--session-id", session_id]
        argv += ["--prompt-file", prompt_path]
        argv += list(spec.extra_args)
        try:
            proc = ProcRun(argv, cwd=spec.cwd, env=spec.child_env())
        except OSError as exc:
            try:
                Path(prompt_path).unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeUnavailable(f"could not launch muse: {exc}") from exc
        run_id = spec.run_id or f"muse-{uuid.uuid4().hex[:8]}"
        handle = RunHandle(runtime_id=self.runtime_id, run_id=run_id,
                           task_id=spec.task_id, pid=proc.pid,
                           session_ref=session_id, proc=proc)
        self._prompt_files[run_id] = prompt_path
        self._timeouts[run_id] = spec.timeout_s
        return handle

    def start(self, spec: RunSpec) -> RunHandle:
        session_seed = spec.run_id if _looks_like_uuid(spec.run_id) else ""
        return self._start_with_session(spec, session_seed)

    def stream(self, handle: RunHandle) -> Iterator[RuntimeEvent]:
        try:
            yield from self._stream_events(handle)
        finally:
            self.close(handle)

    def _stream_events(self, handle: RunHandle) -> Iterator[RuntimeEvent]:
        seq = EventSequencer(task_id=handle.task_id, run_id=handle.run_id,
                             runtime_id=self.runtime_id)
        yield seq.emit("process_started", {"pid": handle.pid})
        proc = handle.proc
        if proc is None:
            yield seq.emit("failed", {"exit_code": 127,
                                      "error": "no process attached"})
            return
        terminal = False
        try:
            lines = proc.iter_lines(
                timeout_s=self._timeouts.get(handle.run_id, 600.0))
            for line in lines:
                if handle.cancelled:
                    yield seq.emit("cancelled", {})
                    return
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except ValueError:
                    yield seq.emit("parse_error", {}, raw=stripped)
                    continue
                if isinstance(obj, dict):
                    sid = _extract_session_id(obj)
                    if sid and not handle.session_ref:
                        handle.session_ref = sid
                    event, terminal = _map_record(obj, stripped, seq)
                else:
                    event = seq.emit("status", {"kind": "unknown"},
                                     raw=stripped)
                yield event
        except TimeoutError as exc:
            yield seq.emit("failed", {"exit_code": 124, "error": str(exc)})
            return
        if handle.cancelled:
            yield seq.emit("cancelled", {})
            return
        if terminal:
            return
        try:
            code = proc.wait(timeout_s=5.0)
        except OSError:
            code = None
        if code is None:
            try:
                code = proc.proc.poll()
            except AttributeError:
                code = 1
        if code == 0:
            yield seq.emit("completed", {"exit_code": 0})
        else:
            yield seq.emit("failed", {"exit_code": int(code or 1)})

    def send(self, handle: RunHandle, message: str) -> None:
        raise UnsupportedCapability(
            "muse-code runtime is one-shot; send is unsupported")

    def cancel(self, handle: RunHandle) -> str:
        handle.cancelled = True
        proc = handle.proc
        if proc is not None and hasattr(proc, "kill_tree"):
            try:
                return str(proc.kill_tree())
            except OSError:
                pass
        return f"muse-code run {handle.run_id} cancelled"

    def resume(self, session_ref: str, spec: RunSpec) -> RunHandle:
        if not session_ref:
            raise RuntimeUnavailable("cannot resume without a session ref")
        return self._start_with_session(replace(spec), session_ref)

    def close(self, handle: RunHandle) -> None:
        prompt_path = self._prompt_files.pop(handle.run_id, "")
        self._timeouts.pop(handle.run_id, None)
        if prompt_path:
            try:
                Path(prompt_path).unlink(missing_ok=True)
            except OSError:
                pass
