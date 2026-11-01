"""Adapter tests for MuseCodeAdapter: argv shape, JSONL mapping, lifecycle.

Deterministic and offline: the child process is faked by monkeypatching
``ProcRun`` (plus ``find_executable``/``probe_version``). No real ``muse``
CLI is launched and no network or API keys are needed.
"""

import json
from pathlib import Path

import pytest

from hubble.runtimes.adapters import muse_code
from hubble.runtimes.adapters.muse_code import MuseCodeAdapter, approval_mode_for
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
    monkeypatch.setattr(muse_code, "ProcRun", FakeProc)
    monkeypatch.setattr(muse_code, "find_executable", lambda names, extra_paths=None: "muse")
    return FakeProc


def _spec(tmp_path, **kw):
    base = dict(prompt="do the thing", cwd=tmp_path,
                task_id="task-1", run_id="run-1")
    base.update(kw)
    return RunSpec(**base)


# detect / capabilities

def test_detect_reports_executable_and_version(monkeypatch):
    monkeypatch.setattr(muse_code, "find_executable",
                        lambda names, extra_paths=None: "C:/muse/muse.CMD")
    monkeypatch.setattr(muse_code, "probe_version", lambda exe: "1.4.14")
    det = MuseCodeAdapter().detect()
    assert det.available is True
    assert det.runtime_id == "muse-code"
    assert det.executable == "C:/muse/muse.CMD"
    assert det.version == "1.4.14"


def test_detect_missing_cli_is_unavailable(monkeypatch):
    monkeypatch.setattr(muse_code, "find_executable",
                        lambda names, extra_paths=None: "")
    det = MuseCodeAdapter().detect()
    assert det.available is False
    assert det.diagnostics


def test_detect_empty_version_is_unavailable(monkeypatch):
    monkeypatch.setattr(muse_code, "find_executable",
                        lambda names, extra_paths=None: "muse")
    monkeypatch.setattr(muse_code, "probe_version", lambda exe: "")
    det = MuseCodeAdapter().detect()
    assert det.available is False


def test_capabilities_match_contract():
    caps = MuseCodeAdapter().capabilities()
    assert caps.supports("one_shot")
    assert caps.supports("json_stream")
    assert caps.supports("resume")
    assert caps.supports("cancel")
    assert caps.supports("worktree_flag")
    assert caps.supports("model_override")
    assert caps.supports("effort_override")
    assert not caps.supports("interactive")


def test_approval_mode_mapping_defaults_to_on_request():
    assert approval_mode_for("") == "on-request"
    assert approval_mode_for("whatever-unknown") == "on-request"
    assert approval_mode_for("read-only") == "untrusted"
    assert approval_mode_for("yolo") == "never"
    assert approval_mode_for("full") == "never"


# start: argv shape

def test_start_builds_argv_array_with_allowlisted_env(tmp_path, fake_cli, monkeypatch):
    monkeypatch.setenv("HUBBLE_API_KEY", "super-secret")
    spec = _spec(tmp_path, env_extra={"MUSE_HOME": "C:/muse-home"})
    handle = MuseCodeAdapter().start(spec)
    proc = fake_cli.created[0]
    assert isinstance(proc.argv, list)
    assert all(isinstance(a, str) for a in proc.argv)
    assert proc.argv[:4] == ["muse", "exec", "--json", "--workspace"]
    assert str(tmp_path) in proc.argv
    mode_at = proc.argv.index("--approval-mode")
    assert proc.argv[mode_at + 1] == "on-request"
    prompt_at = proc.argv.index("--prompt-file")
    assert Path(proc.argv[prompt_at + 1]).read_text(encoding="utf-8") == "do the thing"
    assert "--yolo" not in proc.argv
    assert proc.env["MUSE_HOME"] == "C:/muse-home"
    assert "HUBBLE_API_KEY" not in proc.env
    assert "AWS_SECRET_ACCESS_KEY" not in proc.env
    assert handle.pid == 4242
    MuseCodeAdapter().close(handle)


def test_start_readonly_profile_and_overrides(tmp_path, fake_cli):
    spec = _spec(tmp_path, run_id="9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
                 model="m1", effort="high", permission_profile="readonly",
                 extra_args=["--worktree", "wt-1"])
    handle = MuseCodeAdapter().start(spec)
    argv = fake_cli.created[0].argv
    assert argv[argv.index("--approval-mode") + 1] == "untrusted"
    assert argv[argv.index("--model") + 1] == "m1"
    assert argv[argv.index("--reasoning-effort") + 1] == "high"
    assert argv[argv.index("--session-id") + 1] == "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
    assert argv[-2:] == ["--worktree", "wt-1"]
    assert handle.session_ref == "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"


def test_start_non_uuid_run_id_omits_session_id(tmp_path, fake_cli):
    handle = MuseCodeAdapter().start(_spec(tmp_path))
    assert "--session-id" not in fake_cli.created[0].argv
    assert handle.session_ref == ""


def test_start_missing_cli_raises_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(muse_code, "find_executable",
                        lambda names, extra_paths=None: "")
    with pytest.raises(RuntimeUnavailable):
        MuseCodeAdapter().start(_spec(tmp_path))


# stream: JSONL mapping

def test_stream_maps_text_and_unknown_types_and_session(tmp_path, fake_cli):
    fake_cli.script = [
        json.dumps({"type": "text", "text": "hello",
                    "session_id": "sess-abc"}),
        json.dumps({"type": "frobnicator", "n": 1}),
        json.dumps({"type": "result", "exit_code": 0}),
    ]
    adapter = MuseCodeAdapter()
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
    adapter = MuseCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    kinds = [e.event_type for e in events]
    assert kinds == ["process_started", "parse_error", "stdout", "completed"]
    parse = events[1]
    assert "not json" in parse.payload["raw"]
    assert len(parse.payload["raw"]) <= 2000


def test_stream_nonzero_exit_becomes_failed(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "text", "text": "boom"})]
    fake_cli.exit_code = 2
    adapter = MuseCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "failed"
    assert events[-1].payload["exit_code"] == 2


def test_stream_fatal_error_record_becomes_failed(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "error", "fatal": True, "text": "nope"})]
    adapter = MuseCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "failed"


def test_stream_timeout_becomes_failed(tmp_path, fake_cli):
    fake_cli.raise_timeout = True
    adapter = MuseCodeAdapter()
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
    adapter = MuseCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    by_kind = {e.event_type: e for e in adapter.stream(handle)}
    assert by_kind["usage"].payload["usage"] == {"input": 10, "output": 5}
    assert by_kind["approval_required"].payload["text"] == "rm -rf?"


def test_secrets_never_appear_in_events(tmp_path, fake_cli):
    secret = "sk-muse-test-secret-xyz"
    fake_cli.script = [json.dumps({"type": "text", "text": "working"})]
    adapter = MuseCodeAdapter()
    handle = adapter.start(_spec(tmp_path, env_extra={"MUSE_API_KEY": secret}))
    for event in adapter.stream(handle):
        assert secret not in json.dumps(event.to_dict())


# send / cancel / resume / close

def test_send_is_unsupported(tmp_path, fake_cli):
    adapter = MuseCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    with pytest.raises(UnsupportedCapability):
        adapter.send(handle, "hello")


def test_cancel_kills_tree_and_stream_reports_cancelled(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "text", "text": "x"})]
    adapter = MuseCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    msg = adapter.cancel(handle)
    assert "4242" in msg
    assert fake_cli.created[0].killed is True
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "cancelled"


def test_resume_starts_new_exec_carrying_session_ref(tmp_path, fake_cli):
    adapter = MuseCodeAdapter()
    handle = adapter.resume("sess-carry", _spec(tmp_path, run_id="run-9"))
    argv = fake_cli.created[0].argv
    assert argv[argv.index("--session-id") + 1] == "sess-carry"
    assert handle.run_id == "run-9"
    assert handle.session_ref == "sess-carry"
    with pytest.raises(RuntimeUnavailable):
        adapter.resume("", _spec(tmp_path))


def test_close_removes_prompt_file(tmp_path, fake_cli):
    adapter = MuseCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    prompt_path = Path(fake_cli.created[0].argv[
        fake_cli.created[0].argv.index("--prompt-file") + 1])
    assert prompt_path.is_file()
    adapter.close(handle)
    assert not prompt_path.exists()


def test_stream_removes_prompt_file_without_explicit_close(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "text", "text": "hi"})]
    adapter = MuseCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    prompt_path = Path(fake_cli.created[0].argv[
        fake_cli.created[0].argv.index("--prompt-file") + 1])
    assert prompt_path.is_file()
    list(adapter.stream(handle))
    assert not prompt_path.exists()
    assert tmp_path not in prompt_path.parents
