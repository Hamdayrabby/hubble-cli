"""REPL + CLI wiring for orchestration (no real harness launched)."""

import json
import subprocess

import pytest

from hubble.agent import Agent
from hubble.main import build_parser, main
from hubble.permissions import Permissions
from hubble.provider import TurnResult
from hubble.tools import ToolContext


class Provider:
    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, model, messages, **kw):
        return self.turns.pop(0)


SETTINGS = {"model": "m", "max_turns": 1, "max_tokens": 100, "context_window": 100000,
            "auto_compact_ratio": 0, "persona": "code", "web_tools": False}


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _repl(repo):
    from hubble.repl import Repl
    from hubble.session import SessionStore
    from hubble.ui import ReplEvents
    ctx = ToolContext(root=repo.resolve())
    agent = Agent(Provider([TurnResult(text="done")]), dict(SETTINGS), ctx,
                  Permissions(), ReplEvents(ctx))
    return Repl(agent, SessionStore(repo), {"hubble": object()}), ctx


def test_orchestrate_command_registered(tmp_path):
    repl, _ = _repl(tmp_path)
    assert "orchestrate" in repl.command_names()
    assert "orch" in repl.command_names()


def test_orchestrate_runtimes_lists_without_spawning(tmp_path, capsys):
    repl, _ = _repl(tmp_path)
    repl.handle("/orchestrate runtimes")
    out = capsys.readouterr().out
    assert "fake" in out and "muse-code" in out


def test_orchestrate_run_fake_end_to_end(repo):
    repl, ctx = _repl(repo)
    repl.handle("/orch run impl:do the thing --runtime fake")
    assert (repo / ".hubble" / "runs.jsonl").is_file()
    record = json.loads((repo / ".hubble" / "runs.jsonl").read_text().splitlines()[-1])
    assert record["record"]["status"] == "succeeded"
    assert record["record"]["branch"].startswith("hubble/orch-impl-")


def test_main_orchestrate_list_runtimes(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--orchestrate", "--list-runtimes", "--cwd", str(tmp_path)])
    assert exc.value.code == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["runtime"] for r in rows} >= {"fake", "muse-code", "codex", "antigravity"}


def test_parser_accepts_orchestrate_flags():
    args = build_parser().parse_args(["--orchestrate", "--runtime", "codex", "impl:x"])
    assert args.orchestrate is True and args.runtime == "codex"
