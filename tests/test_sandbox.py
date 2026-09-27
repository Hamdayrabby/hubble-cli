import json
import sys
import uuid
from pathlib import Path

import pytest

import hubble.sandbox as sandbox
from hubble.agent import Agent, Events
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.tools import Shell, ToolContext, detect_shell

NATIVE = sandbox.native_kind()
needs_native = pytest.mark.skipif(NATIVE is None, reason="no OS sandbox on this machine")


def test_auto_resolves_to_native_or_off():
    assert sandbox.effective_mode("auto") == ("native" if NATIVE else "off")
    assert sandbox.effective_mode("off") == "off"
    assert sandbox.effective_mode("docker") == "docker"
    assert sandbox.effective_mode("bogus") == "off"


@pytest.mark.skipif(sys.platform != "win32", reason="windows only")
def test_windows_has_no_native_sandbox():
    assert NATIVE is None and "docker" in sandbox.why_unavailable()


def test_seatbelt_profile_limits_writes_and_network():
    profile = sandbox._seatbelt_profile(["/Users/me/proj", '/tmp/we"ird'], network=False)
    assert "(deny file-write*)" in profile and '(subpath "/Users/me/proj")' in profile
    assert '(subpath "/tmp/we\\"ird")' in profile  # quotes escaped, no profile injection
    assert "deny network-outbound" in profile
    assert "network" not in sandbox._seatbelt_profile(["/Users/me/proj"], network=True)


def test_bwrap_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "native_kind", lambda: "bwrap")
    argv = sandbox.wrap(["/bin/sh", "-c"], "echo hi", tmp_path, [], network=False)
    assert argv[:3] == ["bwrap", "--ro-bind", "/"] and "--unshare-net" in argv
    real = str(Path(tmp_path).resolve())
    assert ["--bind", real, real] == argv[argv.index(real) - 1:argv.index(real) + 2]
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]


def test_worktree_git_dirs_are_writable(tmp_path):
    main_git = tmp_path / "repo" / ".git" / "worktrees" / "wt"
    main_git.mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main_git}\n", encoding="utf-8")
    paths = sandbox.writable_paths(wt, [])
    assert str(main_git.resolve()) in [str(Path(p).resolve()) for p in paths]
    assert str((tmp_path / "repo" / ".git").resolve()) in [str(Path(p).resolve()) for p in paths]


@needs_native
def test_native_sandbox_allows_workspace_writes_and_blocks_outside(tmp_path):
    ctx = ToolContext(root=tmp_path, sandbox="native", shell_argv=detect_shell("bash"))
    out = Shell().run({"command": "echo inside > ok.txt && cat ok.txt"}, ctx)
    assert "inside" in out and (tmp_path / "ok.txt").exists()

    outside = Path.home() / f".hubble-sandbox-test-{uuid.uuid4().hex[:8]}"
    try:
        out = Shell().run({"command": f"echo nope > '{outside}'"}, ctx)
        assert not outside.exists()
        assert "unsandboxed: true" in out  # tells the model how to escalate
        out = Shell().run({"command": f"echo yes > '{outside}'", "unsandboxed": True}, ctx)
        assert outside.exists()
    finally:
        outside.unlink(missing_ok=True)


class RecordingEvents(Events):
    def __init__(self):
        self.asked = []

    def ask(self, tool, args, preview):
        self.asked.append(preview)
        return "no", ""


class Provider:
    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, model, messages, **kw):
        return self.turns.pop(0)


def test_unsandboxed_shell_always_asks_even_with_allow_rule(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "native_kind", lambda: "bwrap")
    call = ToolCall("c1", "shell", json.dumps({"command": "pip install --user x", "unsandboxed": True}))
    events = RecordingEvents()
    agent = Agent(Provider([TurnResult(tool_calls=[call]), TurnResult(text="ok")]),
                  {"model": "m", "max_turns": 3, "max_tokens": 10, "context_window": 1000,
                   "auto_compact_ratio": 0, "persona": "code", "web_tools": False},
                  ToolContext(root=tmp_path, sandbox="auto"),
                  Permissions("accept-edits", allow=["shell(pip*)"]), events)
    agent.run("install it")
    assert events.asked and "outside the sandbox" in events.asked[0]
