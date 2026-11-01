"""Coordinator tests: fake runtime in a real git worktree (offline)."""

import shutil
import subprocess

import pytest

from hubble.orchestration.coordinator import Coordinator
from hubble.orchestration.graph import Task, TaskGraph
from hubble.orchestration.scheduler import Scheduler
from hubble.runtimes.adapters.fake import FakeAdapter
from hubble.runtimes.registry import RuntimeRegistry

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _coordinator(repo, fake):
    reg = RuntimeRegistry()
    reg.register(fake)
    return Coordinator(repo, reg)


def test_writer_gets_worktree_and_base_sha(repo):
    coord = _coordinator(repo, FakeAdapter())
    record = coord.execute(Task("impl", "change things", required_capabilities=["one_shot"]))
    assert record.status == "succeeded"
    assert len(record.base_sha) == 40
    assert record.branch.startswith("hubble/orch-impl-")
    assert "orch-impl-" in record.worktree_path
    assert "porcelain" in record.detail and "diffstat" in record.detail
    assert record.detail["events"] >= 3 and record.detail["tail"] != []


def test_failed_runtime_marks_failed_with_class(repo):
    coord = _coordinator(repo, FakeAdapter(exit_code=2))
    record = coord.execute(Task("impl", "break things", required_capabilities=["one_shot"]))
    assert record.status == "failed" and record.failure_class


def test_scheduler_end_to_end_with_coordinator(repo, tmp_path):
    from hubble.runs.store import RunStore
    coord = _coordinator(repo, FakeAdapter())
    graph = TaskGraph([
        Task("audit", "look", task_type="audit"),
        Task("impl", "change", dependencies=["audit"], required_capabilities=["one_shot"]),
    ])
    store = RunStore(tmp_path / "runs.jsonl")
    out = Scheduler(graph, coord.execute, store=store, max_concurrency=2).run()
    assert out == {"audit": "succeeded", "impl": "succeeded"}
    assert len(RunStore(tmp_path / "runs.jsonl").load()) == 2
