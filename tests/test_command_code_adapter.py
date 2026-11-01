"""Adapter tests for CommandCodeAdapter: argv shape, JSONL mapping, lifecycle.

Deterministic and offline: the child process is faked by monkeypatching
``ProcRun`` (plus ``find_executable``/``probe_version``). No real
command-code CLI is launched and no network or API keys are needed.
"""

import json

import pytest

from hubble.runtimes.adapters import command_code
from hubble.runtimes.adapters.command_code import (
    CommandCodeAdapter,
    permission_mode_for,
    strip_ansi,
)
from hubble.runtimes.protocol import (
    RunSpec,
    RuntimeUnavailable,
    UnsupportedCapability,
)


class FakeProc:
    """Stand-in for ProcRun: records spawn args, replays canned lines."""

    def __init__(self, argv, cwd, env):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.pid = 4242
        self.killed = False
        self.lines = list(FakeProc.script)
        FakeProc.created.append(self)

    def iter_lines(self, timeout_s=600.0):
        if FakeProc.raise_timeout:
            self.killed = True
            raise TimeoutError("runtime timed out after 600s")
        yield from self.lines

    def wait(self, timeout_s=5.0):
        return FakeProc.exit_code

    def kill_tree(self):
        self.killed = True
        return "process 4242 killed"


FakeProc.script = []
FakeProc.exit_code = 0
FakeProc.raise_timeout = False
FakeProc.created = []


@pytest.fixture
def fake_cli(monkeypatch):
    FakeProc.script = []
    FakeProc.exit_code = 0
    FakeProc.raise_timeout = False
    FakeProc.created = []
    monkeypatch.setattr(command_code, "ProcRun", FakeProc)
    monkeypatch.setattr(command_code, "find_executable",
                        lambda names, extra_paths=None: "command-code")
    return FakeProc


def _spec(tmp_path, **kw):
    base = dict(prompt="do the thing", cwd=tmp_path,
                task_id="task-1", run_id="run-1")
    base.update(kw)
    return RunSpec(**base)


# detect / capabilities

def test_detect_reports_executable_and_version(monkeypatch):
    monkeypatch.setattr(command_code, "find_executable",
                        lambda names, extra_paths=None: "C:/cc/command-code.CMD")
    monkeypatch.setattr(command_code, "probe_version",
                        lambda exe: "\x1b[1m1.88.2\x1b[0m")
    det = CommandCodeAdapter().detect()
    assert det.available is True
    assert det.runtime_id == "command-code"
    assert det.executable == "C:/cc/command-code.CMD"
    assert det.version == "1.88.2"
    assert "\x1b" not in det.version


def test_detect_searches_executables_in_order(monkeypatch):
    seen = {}

    def fake_find(names, extra_paths=None):
        seen["names"] = list(names)
        return ""
    monkeypatch.setattr(command_code, "find_executable", fake_find)
    det = CommandCodeAdapter().detect()
    assert det.available is False
    assert seen["names"] == ["command-code", "commandcode", "cmdc", "cmd"]


def test_detect_missing_cli_is_unavailable(monkeypatch):
    monkeypatch.setattr(command_code, "find_executable",
                        lambda names, extra_paths=None: "")
    det = CommandCodeAdapter().detect()
    assert det.available is False
    assert det.diagnostics


def test_detect_empty_version_is_unavailable(monkeypatch):
    monkeypatch.setattr(command_code, "find_executable",
                        lambda names, extra_paths=None: "command-code")
    monkeypatch.setattr(command_code, "probe_version", lambda exe: "   ")
    det = CommandCodeAdapter().detect()
    assert det.available is False


def test_strip_ansi_removes_escape_codes():
    assert strip_ansi("\x1b[32m1.88.2\x1b[0m") == "1.88.2"
    assert strip_ansi("plain") == "plain"


def test_capabilities_match_contract():
    caps = CommandCodeAdapter().capabilities()
    assert caps.supports("one_shot")
    assert caps.supports("json_stream")
    assert caps.supports("resume")
    assert caps.supports("cancel")
    assert caps.supports("worktree_flag")
    assert caps.supports("model_override")
    assert caps.supports("effort_override")
    assert not caps.supports("interactive")


def test_permission_mode_mapping():
    assert permission_mode_for("") == "default"
    assert permission_mode_for("whatever-unknown") == "default"
    assert permission_mode_for("readonly") == "plan"
    assert permission_mode_for("read-only") == "plan"
    assert permission_mode_for("accept") == "accept-edits"
    assert permission_mode_for("accept-edits") == "accept-edits"
    # Never yolo by default: permissive profiles fall back to accept-edits.
    assert permission_mode_for("yolo") == "accept-edits"
    assert permission_mode_for("full") == "accept-edits"


# start: argv shape

def test_start_builds_argv_array_with_allowlisted_env(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("HUBBLE_API_KEY", "super-secret")
    spec = _spec(tmp_path, env_extra={"COMMAND_CODE_HOME": "C:/cc-home"})
    handle = CommandCodeAdapter().start(spec)
    proc = fake_cli.created[0]
    assert isinstance(proc.argv, list)
    assert all(isinstance(a, str) for a in proc.argv)
    assert proc.argv[:2] == ["command-code", "-p"]
    assert proc.argv[2] == "do the thing"
    assert "--output-format" in proc.argv
    assert proc.argv[proc.argv.index("--output-format") + 1] == "json"
    mode_at = proc.argv.index("--permission-mode")
    assert proc.argv[mode_at + 1] == "default"
    assert "--no-auto-update" in proc.argv
    assert "--skip-onboarding" in proc.argv
    assert "--yolo" not in proc.argv
    assert "yolo" not in proc.argv
    # Hubble sets cwd to its worktree: no -w flag by default.
    assert "-w" not in proc.argv
    assert "--worktree" not in proc.argv
    assert proc.env["COMMAND_CODE_HOME"] == "C:/cc-home"
    assert "HUBBLE_API_KEY" not in proc.env
    assert "AWS_SECRET_ACCESS_KEY" not in proc.env
    assert handle.pid == 4242
    CommandCodeAdapter().close(handle)


def test_start_readonly_profile_and_overrides(tmp_path, fake_cli):
    spec = _spec(tmp_path, model="m1", effort="high",
                 permission_profile="readonly",
                 extra_args=["--max-turns", "5"])
    handle = CommandCodeAdapter().start(spec)
    argv = fake_cli.created[0].argv
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("-m") + 1] == "m1"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[-2:] == ["--max-turns", "5"]
    assert handle.session_ref == ""


def test_start_accept_profile_maps_to_accept_edits(tmp_path, fake_cli):
    handle = CommandCodeAdapter().start(
        _spec(tmp_path, permission_profile="accept"))
    argv = fake_cli.created[0].argv
    assert argv[argv.index("--permission-mode") + 1] == "accept-edits"
    assert "yolo" not in argv
    assert handle.session_ref == ""


def test_start_missing_cli_raises_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(command_code, "find_executable",
                        lambda names, extra_paths=None: "")
    with pytest.raises(RuntimeUnavailable):
        CommandCodeAdapter().start(_spec(tmp_path))


# stream: NDJSON mapping

def test_stream_maps_text_and_unknown_types_and_session(tmp_path, fake_cli):
    fake_cli.script = [
        json.dumps({"type": "text", "text": "hello",
                    "session_id": "sess-abc"}),
        json.dumps({"type": "frobnicator", "n": 1}),
        json.dumps({"type": "result", "exit_code": 0}),
    ]
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    kinds = [e.event_type for e in events]
    assert kinds[0] == "process_started"
    assert ("stdout", {"text": "hello"}) in [
        (e.event_type, e.payload) for e in events]
    unknown = [e for e in events if e.event_type == "status"]
    assert len(unknown) == 1
    assert unknown[0].payload["kind"] == "frobnicator"
    assert len(unknown[0].payload["raw"]) <= 2000
    assert kinds[-1] == "completed"
    assert handle.session_ref == "sess-abc"
    assert [e.sequence for e in events] == [1, 2, 3, 4]


def test_stream_malformed_lines_become_parse_errors_not_swallowed(tmp_path, fake_cli):
    fake_cli.script = ["not json at all {{{", json.dumps({"type": "text", "text": "ok"})]
    fake_cli.exit_code = 0
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    kinds = [e.event_type for e in events]
    assert kinds == ["process_started", "parse_error", "stdout", "completed"]
    parse = events[1]
    assert "not json" in parse.payload["raw"]
    assert len(parse.payload["raw"]) <= 2000


def test_stream_result_line_failed_by_exit_code(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "result", "exit_code": 3,
                                   "error": "boom"})]
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "failed"
    assert events[-1].payload["exit_code"] == 3


def test_stream_nonzero_exit_becomes_failed(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "text", "text": "boom"})]
    fake_cli.exit_code = 2
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "failed"
    assert events[-1].payload["exit_code"] == 2


def test_stream_fatal_error_record_becomes_failed(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "error", "fatal": True, "text": "nope"})]
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "failed"


def test_stream_timeout_becomes_failed(tmp_path, fake_cli):
    fake_cli.raise_timeout = True
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path, timeout_s=1))
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "failed"
    assert "timed out" in events[-1].payload["error"]


def test_stream_extracts_usage_and_approval(tmp_path, fake_cli):
    fake_cli.script = [
        json.dumps({"type": "token_usage", "input": 10, "output": 5}),
        json.dumps({"type": "approval_required", "text": "rm -rf?"}),
        json.dumps({"type": "result", "exit_code": 0}),
    ]
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    by_kind = {e.event_type: e for e in adapter.stream(handle)}
    assert by_kind["usage"].payload["usage"] == {"input": 10, "output": 5}
    assert by_kind["approval_required"].payload["text"] == "rm -rf?"


def test_secrets_never_appear_in_events(tmp_path, fake_cli):
    secret = "[REDACTED]"
    fake_cli.script = [json.dumps({"type": "text", "text": "working"})]
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path, env_extra={"COMMAND_CODE_API_KEY": secret}))
    for event in adapter.stream(handle):
        assert secret not in json.dumps(event.to_dict())


def test_raw_payload_is_bounded(tmp_path, fake_cli):
    big = "x" * 5000
    fake_cli.script = [json.dumps({"type": "weird-kind", "blob": big})]
    fake_cli.exit_code = 0
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    for event in events:
        raw = event.payload.get("raw", "")
        assert len(raw) <= 2000


# send / cancel / resume / close

def test_send_is_unsupported(tmp_path, fake_cli):
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    with pytest.raises(UnsupportedCapability):
        adapter.send(handle, "hello")


def test_cancel_kills_tree_and_stream_reports_cancelled(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "text", "text": "x"})]
    adapter = CommandCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    msg = adapter.cancel(handle)
    assert "4242" in msg
    assert fake_cli.created[0].killed is True
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "cancelled"


def test_resume_starts_new_run_carrying_session_ref(tmp_path, fake_cli):
    adapter = CommandCodeAdapter()
    handle = adapter.resume("sess-carry", _spec(tmp_path, run_id="run-9"))
    argv = fake_cli.created[0].argv
    assert argv[argv.index("--session") + 1] == "sess-carry"
    assert "-p" in argv
    assert handle.run_id == "run-9"
    assert handle.session_ref == "sess-carry"
    with pytest.raises(RuntimeUnavailable):
        adapter.resume("", _spec(tmp_path))


def test_stream_without_process_fails(tmp_path):
    from hubble.runtimes.protocol import RunHandle
    adapter = CommandCodeAdapter()
    handle = RunHandle(runtime_id="command-code", run_id="run-x",
                       task_id="task-1", pid=0, proc=None)
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "failed"
