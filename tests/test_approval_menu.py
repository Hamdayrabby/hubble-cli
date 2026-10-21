import io
import sys
import threading
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

import hubble.ui as ui
from hubble.tools import Shell, ToolContext

requires_win32_tty = pytest.mark.skipif(
    sys.platform == "win32" and not sys.stdin.isatty(),
    reason="interactive pipe input tests require an active tty on windows"
)


def run_with_keys(fn, *keys, delay=0.15):
    """Run fn() inside a fake terminal, typing `keys` one after another."""
    with create_pipe_input() as inp:
        def typer():
            for k in keys:
                time.sleep(delay)
                inp.send_text(k)
        threading.Thread(target=typer, daemon=True).start()
        with create_app_session(input=inp, output=DummyOutput()):
            return fn()


OPTS = ["Yes", "Yes, and don't ask again", "No, and tell the model"]


@requires_win32_tty
@pytest.mark.parametrize("keys,expected", [
    (("\r",), 0),                    # Enter on the default
    (("\x1b[B", "\r"), 1),           # down arrow, Enter
    (("\x1b[B", "\x1b[B", "\x1b[B", "\r"), 0),   # wraps around
    (("3",), 2),                     # number answers at once
    (("a",), 1),                     # old one-letter answers still work
    (("\x1b",), 2),                  # Esc = the "no" option
    (("\x03",), None),               # Ctrl+C = stop the turn
])
def test_choose(keys, expected):
    assert run_with_keys(lambda: ui.choose("Run this command?", OPTS, esc_index=2), *keys) == expected


def terminal_events(monkeypatch, tmp_path):
    con = Console(file=io.StringIO(), width=100, force_terminal=True, color_system=None, legacy_windows=False)
    monkeypatch.setattr(ui, "console", con)
    monkeypatch.setattr(ui.sys.stdin, "isatty", lambda: True, raising=False)
    return ui.ReplEvents(ToolContext(root=tmp_path)), con


@requires_win32_tty
def test_approval_yes_always_and_no_with_feedback(monkeypatch, tmp_path):
    ev, con = terminal_events(monkeypatch, tmp_path)
    shell, args = Shell(), {"command": "pytest -q"}
    assert run_with_keys(lambda: ev.ask(shell, args, "pytest -q"), "1") == ("yes", "")
    assert run_with_keys(lambda: ev.ask(shell, args, "pytest -q"), "2") == ("always", "")
    assert run_with_keys(lambda: ev.ask(shell, args, "pytest -q"), "3", "use make test instead\r") == \
        ("no", "use make test instead")
    out = con.file.getvalue()
    assert "Run this command?" not in out  # the menu erases itself; the outcome line remains
    assert "allowed; won't ask again for `pytest` commands this session" in out
    assert "denied: use make test instead" in out


@requires_win32_tty
def test_ctrl_c_in_menu_stops_the_turn(monkeypatch, tmp_path):
    ev, _ = terminal_events(monkeypatch, tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_with_keys(lambda: ev.ask(Shell(), {"command": "ls"}, "ls"), "\x03")


def test_always_label_matches_the_rule_that_gets_saved():
    assert ui._command_family("git status -s") == "`git status` commands"
    assert ui._command_family("rm -rf build && make") == "this exact command"


def test_render_choose_items_formatting():
    opts = ["Yes", "Always", "No"]
    ft = ui._render_choose_items("Run this command?", opts, selected_idx=0, esc_index=2)
    raw = "".join(text for _, text in ft)
    # Breathing room: empty line between question and options, empty line before footer
    assert " Run this command?\n\n" in raw
    assert "\n\n  Enter to select · 1-9 to choose · Esc to cancel · Ctrl+C to stop" in raw

    # Identical 3-character prefix column width for vertical alignment of numbers
    assert " ❯ 1. Yes" in raw
    assert "   2. Always" in raw
    assert "   3. No" in raw

    # Styles
    styles = {text: style for style, text in ft}
    assert styles[" ❯ 1. Yes"] == "bold #00d7ff"
    assert styles["   2. Always"] == ""
    assert styles["  (esc)\n"] == "fg:ansigray"


def test_render_choose_items_selection_shift():
    opts = ["Option A", "Option B"]
    ft = ui._render_choose_items("Choose one:", opts, selected_idx=1, esc_index=0)
    styles = {text: style for style, text in ft}
    assert styles["   1. Option A"] == ""
    assert styles[" ❯ 2. Option B"] == "bold #00d7ff"
    assert styles["  (esc)\n"] == "fg:ansigray"


def test_ask_panel_and_question_politeness(monkeypatch, tmp_path):
    ev, con = terminal_events(monkeypatch, tmp_path)
    shell, args = Shell(), {"command": "git log"}
    # Mock choose to immediately return yes (0) without running interactive app
    monkeypatch.setattr(ui, "choose", lambda q, opts, esc_index=None: 0)
    ev.ask(shell, args, "git log")
    out = con.file.getvalue()
    # Panel rendered with rounded box and allowed confirmation
    assert "allowed" in out


def test_ask_routes_through_approval_bridge(monkeypatch, tmp_path):
    ev, con = terminal_events(monkeypatch, tmp_path)
    shell, args = Shell(), {"command": "pytest"}
    called = []
    ev.approval_bridge = lambda tool, a, prev: called.append((tool.name, a["command"], prev)) or ("yes", "")
    ans, fb = ev.ask(shell, args, "pytest")
    assert ans == "yes" and fb == ""
    assert len(called) == 1
    assert called[0] == ("shell", "pytest", "pytest")


def test_prompt_message_agent_running_status(tmp_path):
    from hubble.agent import Agent
    from hubble.permissions import Permissions
    from hubble.repl import Repl
    from hubble.session import SessionStore
    from hubble.tools import ToolContext

    ctx = ToolContext(root=tmp_path)
    events = ui.ReplEvents(ctx)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(), events)
    repl = Repl(agent, SessionStore(tmp_path), {"test": object()})

    repl.is_busy = True
    # 1. When executing a tool
    events.current_tool = "Fetching https://pypi.org/project/hubble-cli"
    msg = repl._prompt_message()
    msg_text = "".join(text for _, text in msg)
    assert "Fetching https://pypi.org/project/hubble-cli" in msg_text
    assert "❯" in msg_text

    # 2. When giving responses / thinking (Claude Code rotating verbs)
    events.current_tool = None
    events.current_activity = None
    msg2 = repl._prompt_message()
    msg2_text = "".join(text for _, text in msg2)
    assert any(v in msg2_text for v in ["Thinking", "Synthesizing", "Analyzing", "Pondering"])
    assert "❯" in msg2_text

    # 3. When a subagent is running
    events.subagent_start("sub-1", "research: task")
    msg3 = repl._prompt_message()
    msg3_text = "".join(text for _, text in msg3)
    assert "1 subagent running" in msg3_text
    assert "❯" in msg3_text


