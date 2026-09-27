import json
import shutil
import subprocess

import pytest

from hubble import worktree
from hubble.agent import Agent, Events
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.tools import ToolContext

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

SETTINGS = {"model": "m", "max_turns": 6, "max_tokens": 100, "context_window": 100000,
            "auto_compact_ratio": 0, "persona": "code", "web_tools": False}


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def test_create_list_remove(repo):
    info = worktree.create(repo, "feature")
    path = repo / ".hubble" / "worktrees" / "feature"
    assert info["branch"] == "hubble/feature" and path.is_dir() and (path / "app.py").exists()
    names = [w["name"] for w in worktree.list_worktrees(repo)]
    assert "feature" in names
    # Excluded, so the main checkout does not see the worktree as untracked files.
    assert git(repo, "status", "--porcelain") == ""
    assert worktree.main_repo(path) == repo.resolve()
    (path / "new.txt").write_text("wip", encoding="utf-8")
    with pytest.raises(worktree.WorktreeError, match="uncommitted"):
        worktree.remove(repo, "feature")
    worktree.remove(repo, "feature", force=True, delete_branch=True)
    assert not path.exists()


def test_not_a_repo(tmp_path):
    with pytest.raises(worktree.WorktreeError, match="not inside a git repository"):
        worktree.create(tmp_path, "x")


def test_repl_worktree_switch_and_exit(repo):
    from hubble.repl import Repl
    from hubble.session import SessionStore
    from hubble.ui import ReplEvents
    ctx = ToolContext(root=repo.resolve())
    agent = Agent(Provider([]), dict(SETTINGS), ctx, Permissions(), ReplEvents(ctx))
    repl = Repl(agent, SessionStore(repo), {"hubble": object()})
    repl.handle("/worktree new spike")
    assert ctx.root == (repo / ".hubble" / "worktrees" / "spike").resolve()
    assert str(ctx.root) in agent.system_prompt()
    repl.handle("/worktree exit")
    assert ctx.root == repo.resolve()
    repl.handle("/worktree switch spike")
    assert ctx.root.name == "spike"
    repl.handle("/worktree remove spike")
    assert ctx.root == repo.resolve() and not (repo / ".hubble" / "worktrees" / "spike").exists()


class Provider:
    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, model, messages, **kw):
        return self.turns.pop(0)


def test_isolated_edit_subagent_commits_to_its_own_branch(repo):
    provider = Provider([
        TurnResult(tool_calls=[ToolCall("c1", "task", json.dumps({
            "description": "add feature", "prompt": "create feature.py", "capability": "edit",
            "isolation": "worktree"}))]),
        TurnResult(tool_calls=[ToolCall("s1", "write_file", json.dumps({"path": "feature.py", "content": "y = 2\n"}))]),
        TurnResult(text="Created feature.py."),
        TurnResult(text="done"),
    ])

    class NoAsk(Events):
        def ask(self, tool, args, preview):
            raise AssertionError("edits inside an isolated worktree should not need approval")

    agent = Agent(provider, dict(SETTINGS), ToolContext(root=repo), Permissions(), NoAsk())
    agent.run("add a feature in isolation")
    assert not (repo / "feature.py").exists()  # main checkout untouched
    report = next(m for m in agent.messages if m.get("role") == "tool")["content"]
    assert "hubble/task-" in report and "cherry-pick" in report
    branch = next(w["branch"] for w in worktree.list_worktrees(repo) if w["name"].startswith("task-"))
    assert git(repo, "show", f"{branch}:feature.py") == "y = 2"
