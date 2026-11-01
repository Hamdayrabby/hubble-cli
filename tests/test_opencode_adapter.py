"""Adapter tests for OpenCodeAdapter (``opencode run --format json``).

Deterministic and offline: the child process is faked by monkeypatching
``ProcRun`` (plus ``find_executable``/``probe_version``). No real
``opencode`` CLI is launched and no network or API keys are needed.
"""

import json
from pathlib import Path

import pytest

from hubble.runtimes.adapters import opencode as opencode_mod
from hubble.runtimes.adapters.opencode import OpenCodeAdapter, wants_auto_approve
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
    monkeypatch.setattr(opencode_mod, "ProcRun", FakeProc)
    monkeypatch.setattr(opencode_mod, "find_executable",
                        lambda names, extra_paths=None: "opencode")
    return FakeProc


def _spec(tmp_path, **kw):
    base = dict(prompt="do the thing", cwd=tmp_path,
                task_id="task-1", run_id="run-1")
    base.update(kw)
    return RunSpec(**base)


# detect / capabilities

def test_detect_reports_executable_and_version(monkeypatch):
    monkeypatch.setattr(opencode_mod, "find_executable",
                        lambda names, extra_paths=None: "opencode")
    monkeypatch.setattr(opencode_mod, "probe_version",
                        lambda exe, args=None: "1.2.3")
    det = OpenCodeAdapter().detect()
    assert det.available is True
    assert det.runtime_id == "opencode"
    assert det.executable == "opencode"
    assert det.version == "1.2.3"


def test_detect_missing_cli_is_unavailable(monkeypatch):
    monkeypatch.setattr(opencode_mod, "find_executable",
                        lambda names, extra_paths=None: "")
    det = OpenCodeAdapter().detect()
    assert det.available is False
    assert det.runtime_id == "opencode"
    assert det.diagnostics


def test_detect_empty_version_is_unavailable(monkeypatch):
    monkeypatch.setattr(opencode_mod, "find_executable",
                        lambda names, extra_paths=None: "opencode")
    monkeypatch.setattr(opencode_mod, "probe_version",
                        lambda exe, args=None: "")
    det = OpenCodeAdapter().detect()
    assert det.available is False


def test_capabilities_match_contract():
    caps = OpenCodeAdapter().capabilities()
    assert caps.supports("one_shot")
    assert caps.supports("json_stream")
    assert caps.supports("resume")
    assert caps.supports("cancel")
    assert caps.supports("model_override")
    assert not caps.supports("interactive")


def test_auto_approve_mapping_defaults_to_off():
    assert wants_auto_approve("") is False
    assert wants_auto_approve("whatever-unknown") is False
    assert wants_auto_approve("readonly") is False
    assert wants_auto_approve("auto") is True
    assert wants_auto_approve("full") is True
    assert wants_auto_approve("yolo") is True


# start: argv shape

def test_start_builds_argv_array_with_allowlisted_env(tmp_path, fake_cli,
                                                      monkeypatch):
    monkeypatch.setenv("HUBBLE_API_KEY", "super-secret")
    spec = _spec(tmp_path, env_extra={"OPENCODE_HOME": "C:/opencode-home"})
    handle = OpenCodeAdapter().start(spec)
    proc = fake_cli.created[0]
    assert isinstance(proc.argv, list)
    assert all(isinstance(a, str) for a in proc.argv)
    assert proc.argv[:4] == ["opencode", "run", "--format", "json"]
    assert proc.argv[-1] == "do the thing"
    assert "--auto" not in proc.argv
    assert "--yolo" not in proc.argv
    assert str(proc.cwd) == str(tmp_path)
    assert proc.env["OPENCODE_HOME"] == "C:/opencode-home"
    assert "HUBBLE_API_KEY" not in proc.env
    assert "AWS_SECRET_ACCESS_KEY" not in proc.env
    assert handle.pid == 4242
    assert handle.runtime_id == "opencode"


def test_start_model_and_auto_profile_and_extra_args(tmp_path, fake_cli):
    spec = _spec(tmp_path, model="provider/model#variant",
                 permission_profile="auto",
                 extra_args=["--agent", "plan", "--thinking"])
    handle = OpenCodeAdapter().start(spec)
    argv = fake_cli.created[0].argv
    assert argv[argv.index("-m") + 1] == "provider/model#variant"
    assert "--auto" in argv
    assert argv[-4:-1] == ["--agent", "plan", "--thinking"]
    assert argv[-1] == "do the thing"
    assert handle.session_ref == ""


def test_start_strips_forbidden_flags_but_keeps_explicit_auto(tmp_path,
                                                              fake_cli):
    spec = _spec(tmp_path,
                 extra_args=["--yolo", "--auto", "--agent", "x"])
    OpenCodeAdapter().start(spec)
    argv = fake_cli.created[0].argv
    assert "--yolo" not in argv
    assert "--auto" in argv  # explicitly requested via extra_args
    assert argv[argv.index("--agent") + 1] == "x"


def test_start_missing_cli_raises_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_mod, "find_executable",
                        lambda names, extra_paths=None: "")
    with pytest.raises(RuntimeUnavailable):
        OpenCodeAdapter().start(_spec(tmp_path))


# stream: JSONL mapping

def test_stream_maps_text_and_unknown_types_and_session(tmp_path, fake_cli):
    fake_cli.script = [
        json.dumps({"type": "text", "text": "hello",
                    "session_id": "sess-abc"}),
        json.dumps({"type": "frobnicator", "n": 1}),
        json.dumps({"type": "result", "exit_code": 0}),
    ]
    adapter = OpenCodeAdapter()
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


def test_stream_harvests_nested_session_object(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "message",
                                   "session": {"id": "sess-nested"},
                                   "content": "hi"})]
    fake_cli.exit_code = 0
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "completed"
    assert handle.session_ref == "sess-nested"


def test_stream_malformed_lines_become_parse_errors_not_swallowed(tmp_path,
                                                                  fake_cli):
    fake_cli.script = ["not json at all {{{",
                       json.dumps({"type": "text", "text": "ok"})]
    fake_cli.exit_code = 0
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    kinds = [e.event_type for e in events]
    assert kinds == ["process_started", "parse_error", "stdout", "completed"]
    parse = events[1]
    assert "not json" in parse.payload["raw"]
    assert len(parse.payload["raw"]) <= 2000


def test_stream_non_dict_json_becomes_status(tmp_path, fake_cli):
    fake_cli.script = ["[1, 2, 3]"]
    fake_cli.exit_code = 0
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    assert events[1].event_type == "status"
    assert events[-1].event_type == "completed"


def test_stream_nonzero_exit_becomes_failed(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "text", "text": "boom"})]
    fake_cli.exit_code = 2
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "failed"
    assert events[-1].payload["exit_code"] == 2


def test_stream_fatal_error_record_becomes_failed(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "error", "fatal": True,
                                   "error": "nope", "exit_code": 1})]
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "failed"


def test_stream_nonfatal_error_record_becomes_stderr(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "error", "error": "warn only"}),
                       json.dumps({"type": "text", "text": "carrying on"})]
    fake_cli.exit_code = 0
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    kinds = [e.event_type for e in events]
    assert "stderr" in kinds
    assert kinds[-1] == "completed"


def test_stream_timeout_becomes_failed(tmp_path, fake_cli):
    fake_cli.raise_timeout = True
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path, timeout_s=1))
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "failed"
    assert "timed out" in events[-1].payload["error"]


def test_stream_extracts_usage(tmp_path, fake_cli):
    fake_cli.script = [
        json.dumps({"type": "usage", "input_tokens": 10,
                    "output_tokens": 5}),
        json.dumps({"type": "result", "exit_code": 0}),
    ]
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    by_kind = {e.event_type: e for e in adapter.stream(handle)}
    assert by_kind["usage"].payload["usage"] == {"input_tokens": 10,
                                                 "output_tokens": 5}


def test_secrets_never_appear_in_events(tmp_path, fake_cli):
    secret = "sk-abcdefgh12345678"
    fake_cli.script = [json.dumps({"type": "text", "text": "working"})]
    adapter = OpenCodeAdapter()
    handle = adapter.start(
        _spec(tmp_path, env_extra={"OPENCODE_API_KEY": secret}))
    for event in adapter.stream(handle):
        assert secret not in json.dumps(event.to_dict())


def test_raw_bounded_to_2000(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "weird-kind-" + "z" * 5000})]
    fake_cli.exit_code = 0
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    status = [e for e in events if e.event_type == "status"]
    assert len(status) == 1
    assert len(status[0].payload["raw"]) <= 2000


# send / cancel / resume / close

def test_send_is_unsupported(tmp_path, fake_cli):
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    with pytest.raises(UnsupportedCapability):
        adapter.send(handle, "hello")


def test_cancel_kills_tree_and_stream_reports_cancelled(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "text", "text": "x"})]
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    msg = adapter.cancel(handle)
    assert "4242" in msg
    assert fake_cli.created[0].killed is True
    events = list(adapter.stream(handle))
    assert events[-1].event_type == "cancelled"


def test_resume_starts_new_run_carrying_session_ref(tmp_path, fake_cli):
    adapter = OpenCodeAdapter()
    handle = adapter.resume("sess-carry", _spec(tmp_path, run_id="run-9"))
    argv = fake_cli.created[0].argv
    assert argv[argv.index("-s") + 1] == "sess-carry"
    assert "--fork" not in argv
    assert handle.run_id == "run-9"
    assert handle.session_ref == "sess-carry"
    with pytest.raises(RuntimeUnavailable):
        adapter.resume("", _spec(tmp_path))


def test_resume_fork_adds_fork_flag(tmp_path, fake_cli):
    adapter = OpenCodeAdapter()
    handle = adapter.resume("sess-fork", _spec(tmp_path), fork=True)
    argv = fake_cli.created[0].argv
    assert argv[argv.index("-s") + 1] == "sess-fork"
    assert "--fork" in argv
    assert handle.session_ref == "sess-fork"


def test_all_events_carry_runtime_id(tmp_path, fake_cli):
    fake_cli.script = [json.dumps({"type": "text", "text": "hi"})]
    fake_cli.exit_code = 0
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    events = list(adapter.stream(handle))
    assert events
    assert all(e.runtime_id == "opencode" for e in events)


def test_close_drops_tracked_spec(tmp_path, fake_cli):
    adapter = OpenCodeAdapter()
    handle = adapter.start(_spec(tmp_path))
    assert handle.run_id in adapter._specs
    adapter.close(handle)
    assert handle.run_id not in adapter._specs
    assert Path(tmp_path).is_dir()
