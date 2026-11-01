"""Adapter tests for the Codex runtime (``codex exec --json``).

Fully offline: the subprocess layer (``ProcRun``) and discovery helpers are
monkeypatched, so no real ``codex`` binary is launched and no credentials or
network are needed.
"""

import json

import pytest

from hubble.runtimes.adapters import codex as codex_mod
from hubble.runtimes.adapters.codex import CodexAdapter
from hubble.runtimes.protocol import (
    RunSpec,
    RuntimeUnavailable,
    UnsupportedCapability,
)

FAKE_EXE = "C:\\fake\\bin\\codex.exe"


class FakeProc:
    """Test double for ProcRun: replays canned stdout lines."""

    lines = ()
    rc = 0
    instances = []

    def __init__(self, argv, cwd, env):
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env)
        self.killed = False
        type(self).instances.append(self)

    @property
    def pid(self):
        return 4242

    def iter_lines(self, timeout_s=600.0):
        yield from type(self).lines

    def wait(self, timeout_s=5.0):
        return type(self).rc

    def kill_tree(self):
        self.killed = True
        return "killed"


def _patch_proc(monkeypatch, lines=(), rc=0):
    fake = type("FakeProcCase", (FakeProc,), {"lines": tuple(lines),
                                              "rc": rc, "instances": []})
    monkeypatch.setattr(codex_mod, "ProcRun", fake)
    monkeypatch.setattr(codex_mod, "find_executable",
                        lambda names, extra_paths=None: FAKE_EXE)
    return fake, fake.instances


def _spec(tmp_path, **kw):
    base = dict(prompt="do the thing", cwd=tmp_path,
                task_id="task-1", run_id="run-1")
    base.update(kw)
    return RunSpec(**base)


def test_runtime_id_and_capabilities():
    adapter = CodexAdapter()
    assert adapter.runtime_id == "codex"
    caps = adapter.capabilities()
    assert caps.one_shot and caps.json_stream
    assert caps.resume and caps.cancel and caps.worktree_flag
    assert caps.model_override
    assert caps.sandbox_modes == ["read-only", "workspace-write",
                                  "danger-full-access"]


def test_start_argv_defaults(tmp_path, monkeypatch):
    _, instances = _patch_proc(monkeypatch)
    handle = CodexAdapter().start(_spec(tmp_path))
    argv = instances[0].argv
    assert argv[:3] == [FAKE_EXE, "exec", "--json"]
    assert "-C" in argv and str(tmp_path) in argv
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert argv[-1] == "do the thing"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert isinstance(argv, list)
    assert handle.pid == 4242 and handle.runtime_id == "codex"


def test_start_read_only_profile_and_model(tmp_path, monkeypatch):
    _, instances = _patch_proc(monkeypatch)
    CodexAdapter().start(_spec(tmp_path, permission_profile="read-only",
                               model="gpt-5"))
    argv = instances[0].argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-m") + 1] == "gpt-5"


def test_start_strips_forbidden_bypass_flag(tmp_path, monkeypatch):
    _, instances = _patch_proc(monkeypatch)
    CodexAdapter().start(_spec(
        tmp_path, extra_args=["--worktree",
                              "--dangerously-bypass-approvals-and-sandbox"]))
    argv = instances[0].argv
    assert "--worktree" in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_start_env_is_allowlisted(tmp_path, monkeypatch):
    _, instances = _patch_proc(monkeypatch, lines=[])
    monkeypatch.setenv("HUBBLE_API_KEY", "super-secret")
    CodexAdapter().start(_spec(tmp_path, env_extra={"CODEX_HOME": "/tmp/cx"}))
    env = instances[0].env
    assert env["CODEX_HOME"] == "/tmp/cx"
    assert "HUBBLE_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_stream_maps_message_to_stdout(tmp_path, monkeypatch):
    line = json.dumps({"type": "item.completed",
                       "item": {"content": [{"text": "hello world"}]}})
    _patch_proc(monkeypatch, lines=[line + "\n"])
    adapter = CodexAdapter()
    events = list(adapter.stream(adapter.start(_spec(tmp_path))))
    bodies = [(e.event_type, e.payload) for e in events]
    assert bodies[0][0] == "process_started"
    assert ("stdout", {"text": "hello world"}) in bodies
    assert bodies[-1][0] == "completed"


def test_stream_unknown_type_status(tmp_path, monkeypatch):
    big = "x" * 5000
    line = json.dumps({"type": "frobnicate.widgets", "blob": big})
    fake, _ = _patch_proc(monkeypatch, lines=[line + "\n"])
    adapter = CodexAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    by_type = {e.event_type: e for e in events}
    assert by_type["status"].payload["state"] == "frobnicate.widgets"
    assert len(by_type["status"].payload["raw"]) <= 2000


def test_stream_malformed_lines_become_parse_errors(tmp_path, monkeypatch):
    good = json.dumps({"type": "item.completed",
                       "item": {"content": "kept"}}) + "\n"
    _patch_proc(monkeypatch, lines=["not json at all\n", "[1, 2]\n", good])
    adapter = CodexAdapter()
    events = list(adapter.stream(adapter.start(_spec(tmp_path))))
    types = [e.event_type for e in events]
    assert types == ["process_started", "parse_error", "parse_error",
                     "stdout", "completed"]


def test_stream_error_event_and_nonzero_exit(tmp_path, monkeypatch):
    err = json.dumps({"type": "turn.failed", "error": "boom"}) + "\n"
    _patch_proc(monkeypatch, lines=[err], rc=1)
    adapter = CodexAdapter()
    events = list(adapter.stream(adapter.start(_spec(tmp_path))))
    assert events[1].event_type == "failed"
    assert events[1].payload["error"] == "boom"
    assert events[-1].event_type == "failed"
    assert events[-1].payload["exit_code"] == 1


def test_stream_captures_session_ref(tmp_path, monkeypatch):
    line = json.dumps({"type": "thread.started", "thread_id": "sess-9"}) + "\n"
    _patch_proc(monkeypatch, lines=[line])
    adapter = CodexAdapter()
    handle = adapter.start(_spec(tmp_path))
    list(adapter.stream(handle))
    assert handle.session_ref == "sess-9"


def test_stream_secrets_never_leak_into_events(tmp_path, monkeypatch):
    key = "sk-abcdefghijklmnop123"
    line = json.dumps({"type": "mystery", "blob": f"leaked {key} here"})
    _patch_proc(monkeypatch, lines=[line + "\n", "bare " + key + " line\n"])
    adapter = CodexAdapter()
    events = list(adapter.stream(adapter.start(_spec(tmp_path))))
    for event in events:
        assert key not in json.dumps(event.payload)


def test_send_is_unsupported(tmp_path, monkeypatch):
    _patch_proc(monkeypatch)
    adapter = CodexAdapter()
    handle = adapter.start(_spec(tmp_path))
    with pytest.raises(UnsupportedCapability):
        adapter.send(handle, "hello")


def test_resume_argv_and_empty_ref(tmp_path, monkeypatch):
    _, instances = _patch_proc(monkeypatch)
    adapter = CodexAdapter()
    handle = adapter.resume("sess-7", _spec(tmp_path))
    argv = instances[0].argv
    assert argv[:5] == [FAKE_EXE, "exec", "--json", "resume", "sess-7"]
    assert "--last" in argv
    assert handle.session_ref == "sess-7"
    with pytest.raises(ValueError):
        adapter.resume("", _spec(tmp_path))


def test_resume_needs_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_mod, "find_executable",
                        lambda names, extra_paths=None: "")
    with pytest.raises(RuntimeUnavailable):
        CodexAdapter().resume("sess-7", _spec(tmp_path))


def test_cancel_kills_process(tmp_path, monkeypatch):
    _, instances = _patch_proc(monkeypatch, lines=[
        json.dumps({"type": "item.completed",
                    "item": {"content": "late"}}) + "\n"])
    adapter = CodexAdapter()
    handle = adapter.start(_spec(tmp_path))
    msg = adapter.cancel(handle)
    assert "cancelled" in msg
    assert instances[0].killed
    assert list(adapter.stream(handle))[-1].event_type == "cancelled"


def test_detect_unavailable_and_available(monkeypatch):
    monkeypatch.setattr(codex_mod, "find_executable",
                        lambda names, extra_paths=None: "")
    missing = CodexAdapter().detect()
    assert missing.available is False and missing.runtime_id == "codex"

    seen = {}

    def fake_find(names, extra_paths=None):
        seen["extra"] = extra_paths
        return FAKE_EXE

    monkeypatch.setattr(codex_mod, "find_executable", fake_find)
    monkeypatch.setattr(codex_mod, "probe_version",
                        lambda exe, args=None: "codex-cli 0.166.0")
    found = CodexAdapter().detect()
    assert found.available and found.executable == FAKE_EXE
    assert "0.166.0" in found.version
    assert any("codex" in str(p).lower().replace("/", "\\")
               or ".codex" in str(p) for p in seen["extra"])
