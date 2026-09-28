import io
import threading
import time

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

import hubble.ui as ui
from hubble.tools import Shell, ToolContext


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


def test_ctrl_c_in_menu_stops_the_turn(monkeypatch, tmp_path):
    ev, _ = terminal_events(monkeypatch, tmp_path)
    with pytest.raises(KeyboardInterrupt):
        run_with_keys(lambda: ev.ask(Shell(), {"command": "ls"}, "ls"), "\x03")


def test_always_label_matches_the_rule_that_gets_saved():
    assert ui._command_family("git status -s") == "`git status` commands"
    assert ui._command_family("rm -rf build && make") == "this exact command"
