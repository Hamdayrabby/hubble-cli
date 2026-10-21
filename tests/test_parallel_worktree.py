import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hubble import worktree
from hubble.agent import Agent, Events
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.tools import ToolContext

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

SETTINGS = {
    "model": "m",
    "max_turns": 6,
    "max_tokens": 100,
    "context_window": 100000,
    "auto_compact_ratio": 0,
    "persona": "code",
    "web_tools": False,
}


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


class ScriptedProvider:
    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, model, messages, **kw):
        return self.turns.pop(0)


def test_diff_worktree_uncommitted_and_committed(repo):
    info = worktree.create(repo, "feat-diff")
    wt_path = Path(info["path"])

    # Make uncommitted change in worktree
    (wt_path / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    git(wt_path, "add", "calc.py")
    diff1 = worktree.diff_worktree(wt_path, "main")
    assert "calc.py" in diff1 and "+def add(a, b): return a + b" in diff1

    # Commit change in worktree
    worktree.commit_all(wt_path, "add calc.py")
    diff2 = worktree.diff_worktree(wt_path, "main")
    assert "calc.py" in diff2 and "+def add(a, b): return a + b" in diff2

    worktree.remove(repo, "feat-diff", force=True, delete_branch=True)


def test_merge_worktree_squash_success(repo):
    info = worktree.create(repo, "feat-squash")
    wt_path = Path(info["path"])

    # Work in worktree
    (wt_path / "utils.py").write_text("def greet(): return 'hello'\n", encoding="utf-8")
    worktree.commit_all(wt_path, "add utils")

    (wt_path / "utils.py").write_text("def greet(): return 'hello world'\n", encoding="utf-8")
    worktree.commit_all(wt_path, "improve greet")

    # Main repo shouldn't have utils.py yet
    assert not (repo / "utils.py").exists()

    res = worktree.merge_worktree(repo, "feat-squash", squash=True)
    assert res["success"] is True
    assert res["merged"] is True
    assert (repo / "utils.py").exists()
    assert "hello world" in (repo / "utils.py").read_text(encoding="utf-8")
    assert not wt_path.exists()  # Worktree was cleaned up


def test_merge_worktree_no_ff(repo):
    info = worktree.create(repo, "feat-noff")
    wt_path = Path(info["path"])

    (wt_path / "lib.py").write_text("VERSION = '1.0'\n", encoding="utf-8")
    worktree.commit_all(wt_path, "add lib")

    res = worktree.merge_worktree(repo, "feat-noff", squash=False)
    assert res["success"] is True
    assert res["merged"] is True
    assert (repo / "lib.py").exists()
    assert not wt_path.exists()


def test_merge_worktree_conflict_aborts_safely(repo):
    info = worktree.create(repo, "feat-conflict")
    wt_path = Path(info["path"])

    # Worktree modifies app.py
    (wt_path / "app.py").write_text("def run():\n    return 'from worktree'\n", encoding="utf-8")
    worktree.commit_all(wt_path, "worktree change")

    # Main repo also modifies app.py in a conflicting way
    (repo / "app.py").write_text("def run():\n    return 'from main'\n", encoding="utf-8")
    git(repo, "commit", "-a", "-m", "main conflicting change")

    res = worktree.merge_worktree(repo, "feat-conflict", squash=True)
    assert res["success"] is False
    assert res["conflict"] is True
    assert "Merge conflict" in res["error"]

    # Verify main repo is not in a broken merge state
    status = git(repo, "status", "--porcelain")
    assert status == ""
    assert (repo / "app.py").read_text(encoding="utf-8") == "def run():\n    return 'from main'\n"

    worktree.remove(repo, "feat-conflict", force=True, delete_branch=True)


def test_isolated_subagent_auto_merge_synthetic_pr(repo):
    provider = ScriptedProvider([
        TurnResult(tool_calls=[ToolCall("c1", "task", json.dumps({
            "description": "implement payment gateway",
            "prompt": "create payments.py",
            "capability": "edit",
            "isolation": "worktree",
            "auto_merge": True,
        }))]),
        TurnResult(tool_calls=[ToolCall("s1", "write_file", json.dumps({
            "path": "payments.py",
            "content": "class StripeGateway:\n    pass\n",
        }))]),
        TurnResult(text="Created payment gateway module."),
        TurnResult(text="Feature complete and merged."),
    ])

    class NoAsk(Events):
        def ask(self, tool, args, preview):
            raise AssertionError("edits inside worktree should not ask for confirmation")

    agent = Agent(provider, dict(SETTINGS), ToolContext(root=repo), Permissions(), NoAsk())
    agent.run("implement payments in isolation and merge")

    # Main checkout should now have payments.py automatically merged!
    assert (repo / "payments.py").exists()
    assert "StripeGateway" in (repo / "payments.py").read_text(encoding="utf-8")

    # Verify synthetic PR was reported
    tool_out = next(m for m in agent.messages if m.get("role") == "tool")["content"]
    assert "### 🔀 Synthetic PR: implement payment gateway (Merged)" in tool_out
    assert "squash-merged into `main`" in tool_out
    assert "Merge Commit" in tool_out
