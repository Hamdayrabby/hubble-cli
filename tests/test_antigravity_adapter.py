"""Adapter tests for AntigravityAdapter (agy CLI).

Offline: the subprocess layer is faked by monkeypatching ProcRun in the
adapter module. No real CLI is launched; no network or API keys needed.
"""

import pytest

from hubble.runtimes.adapters import antigravity as agy_mod
from hubble.runtimes.adapters.antigravity import AntigravityAdapter
from hubble.runtimes.protocol import (
    RunSpec, RuntimeUnavailable, UnsupportedCapability,
)


class FakeProc:
    script = []
    exit_code = 0
    raise_timeout = False
    instances = []

    def __init__(self, argv, cwd, env):
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.killed = False
        FakeProc.instances.append(self)

    @property
    def pid(self):
        return 4242

    def iter_lines(self, timeout_s=600.0):
        if FakeProc.raise_timeout:
            raise TimeoutError("runtime timed out after 600s")
        return iter(list(FakeProc.script))

    def wait(self, timeout_s=5.0):
        return FakeProc.exit_code

    def kill_tree(self):
        self.killed = True
        return "process 4242 killed"


@pytest.fixture
def fake_proc(monkeypatch):
    FakeProc.script = []
    FakeProc.exit_code = 0
    FakeProc.raise_timeout = False
    FakeProc.instances = []
    monkeypatch.setattr(agy_mod, "ProcRun", FakeProc)
    return FakeProc


def _spec(tmp_path, **kw):
    kw.setdefault("prompt", "do the thing")
    kw.setdefault("task_id", "task-1")
    kw.setdefault("run_id", "run-1")
    return RunSpec(cwd=tmp_path, **kw)


def _adapter():
    return AntigravityAdapter(executable="agy")


def test_capabilities(fake_proc):
    caps = _adapter().capabilities()
    assert caps.one_shot and caps.json_stream
    assert caps.resume and caps.cancel
    assert caps.model_override and caps.effort_override
    assert caps.supports("resume") and caps.supports("json_stream")
    assert not caps.interactive and not caps.worktree_flag


def test_start_argv_defaults_and_env(tmp_path, fake_proc, monkeypatch):
    monkeypatch.setenv("HUBBLE_API_KEY", "super-secret")
    spec = _spec(tmp_path, env_extra={"AGY_HOME": "/tmp/agy"})
    handle = _adapter().start(spec)
    proc = FakeProc.instances[-1]
    assert proc.argv[:5] == ["agy", "-p", "do the thing",
                             "--output-format", "stream-json"]
    assert "--mode" in proc.argv
    assert proc.argv[proc.argv.index("--mode") + 1] == "accept-edits"
    assert "--dangerously-skip-permissions" not in proc.argv
    assert all(isinstance(a, str) for a in proc.argv)
    assert str(proc.cwd) == str(tmp_path)
    assert proc.env["AGY_HOME"] == "/tmp/agy"
    assert "HUBBLE_API_KEY" not in proc.env
    assert handle.pid == 4242 and handle.run_id == "run-1"


def test_permission_profile_maps_to_mode(tmp_path, fake_proc):
    adapter = _adapter()
    assert "--mode" in adapter.build_argv(_spec(tmp_path))
    assert adapter.build_argv(_spec(tmp_path))[
        adapter.build_argv(_spec(tmp_path)).index("--mode") + 1] == "accept-edits"
    by_profile = {
        "": "accept-edits",
        "read-only": "plan",
        "plan": "plan",
        "auto": "auto",
        "accept-edits": "accept-edits",
    }
    for profile, mode in by_profile.items():
        argv = adapter.build_argv(_spec(tmp_path, permission_profile=profile))
        assert argv[argv.index("--mode") + 1] == mode, profile
        assert "--dangerously-skip-permissions" not in argv


def test_model_effort_and_extra_args(tmp_path, fake_proc):
    spec = _spec(tmp_path, model="antigravity-pro", effort="high",
                 extra_args=["--sandbox", "strict"])
    argv = _adapter().build_argv(spec)
    assert argv[argv.index("--model") + 1] == "antigravity-pro"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[-2:] == ["--sandbox", "strict"]


def test_start_unavailable_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(agy_mod, "find_executable", lambda names, extra_paths=None: "")
    adapter = AntigravityAdapter()
    assert adapter.detect().available is False
    with pytest.raises(RuntimeUnavailable):
        adapter.start(_spec(tmp_path))


def test_detect_reports_version(monkeypatch):
    monkeypatch.setattr(agy_mod, "find_executable",
                        lambda names, extra_paths=None: "/bin/agy")
    monkeypatch.setattr(agy_mod, "probe_version",
                        lambda exe, args=None: "agy 1.4.4")
    result = AntigravityAdapter().detect()
    assert result.available and result.executable == "/bin/agy"
    assert result.version == "agy 1.4.4"


def test_stream_maps_events_and_recovers_from_bad_lines(tmp_path, fake_proc):
    FakeProc.script = [
        '{"type": "assistant", "message": {"content": [{"text": "hello"}]},'
        ' "session_id": "sess-9"}',
        '{"type": "error", "message": "boom"}',
        '{"type": "result", "usage": {"input_tokens": 10, "output_tokens": 5}}',
        '{"type": "mystery-widget", "frobnicate": 1}',
        "this is not json {{{",
        '["just", "a", "list"]',
        '{"type": "assistant", "text": "still going"}',
    ]
    adapter = _adapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    kinds = [e.event_type for e in events]
    assert kinds == ["process_started", "stdout", "stderr", "usage", "status",
                     "parse_error", "status", "stdout", "completed"]
    assert events[1].payload["text"] == "hello"
    assert events[2].payload["text"] == "boom"
    assert events[3].payload["usage"]["input_tokens"] == 10
    assert events[4].payload["state"] == "mystery-widget"
    assert len(events[4].payload["raw"]) <= 2000
    assert events[5].event_type == "parse_error"
    assert events[6].payload["state"] == "unknown"
    assert events[7].payload["text"] == "still going"
    assert events[8].payload["exit_code"] == 0
    assert handle.session_ref == "sess-9"
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert all(e.runtime_id == "antigravity" for e in events)


def test_stream_failure_exit_code(tmp_path, fake_proc):
    FakeProc.script = []
    FakeProc.exit_code = 3
    handle = _adapter().start(_spec(tmp_path))
    last = list(_adapter().stream(handle))[-1]
    assert last.event_type == "failed" and last.payload["exit_code"] == 3


def test_stream_timeout_reports_failed(tmp_path, fake_proc):
    FakeProc.raise_timeout = True
    handle = _adapter().start(_spec(tmp_path))
    last = list(_adapter().stream(handle))[-1]
    assert last.event_type == "failed" and last.payload.get("timeout") is True


def test_send_is_unsupported(tmp_path, fake_proc):
    adapter = _adapter()
    handle = adapter.start(_spec(tmp_path))
    with pytest.raises(UnsupportedCapability):
        adapter.send(handle, "hello")


def test_resume_uses_conversation_flag(tmp_path, fake_proc):
    adapter = _adapter()
    handle = adapter.resume("conv-1", _spec(tmp_path))
    proc = FakeProc.instances[-1]
    assert proc.argv[proc.argv.index("--conversation") + 1] == "conv-1"
    assert "--continue" not in proc.argv
    assert handle.session_ref == "conv-1"
    with pytest.raises(RuntimeUnavailable):
        adapter.resume("", _spec(tmp_path))


def test_resume_continue_sentinel(tmp_path, fake_proc):
    adapter = _adapter()
    adapter.resume("continue", _spec(tmp_path))
    assert "--continue" in FakeProc.instances[-1].argv


def test_cancel_kills_tree_and_stream_reports_cancelled(tmp_path, fake_proc):
    FakeProc.script = ['{"type": "assistant", "text": "hi"}']
    adapter = _adapter()
    handle = adapter.start(_spec(tmp_path))
    msg = adapter.cancel(handle)
    assert "killed" in msg and handle.cancelled
    assert FakeProc.instances[-1].killed
    assert list(adapter.stream(handle))[-1].event_type == "cancelled"
