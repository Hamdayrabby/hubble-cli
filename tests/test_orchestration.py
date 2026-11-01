"""Tests: DAG validation, scheduler semantics, run journal, failure taxonomy."""

import pytest

from hubble.evaluation.failures import changed_variables, classify
from hubble.orchestration.graph import GraphError, Task, TaskGraph
from hubble.orchestration.scheduler import Scheduler
from hubble.runs.store import RunRecord, RunStore


def _ok(task):
    return RunRecord(run_id=f"{task.task_id}-a{task.attempts}", task_id=task.task_id,
                     status="succeeded")


def _fail(task):
    return RunRecord(run_id=f"{task.task_id}-a{task.attempts}", task_id=task.task_id,
                     status="failed", failure_class="test_failure")


def test_valid_dag_and_cycle_and_missing_dep():
    graph = TaskGraph([Task("a", "x"), Task("b", "y", dependencies=["a"])])
    assert [t.task_id for t in graph.ready()] == ["a"]
    with pytest.raises(GraphError):
        TaskGraph([Task("a", "x", dependencies=["b"]), Task("b", "y", dependencies=["a"])])
    with pytest.raises(GraphError):
        TaskGraph([Task("a", "x", dependencies=["nope"])])
    with pytest.raises(GraphError):
        TaskGraph([Task("a", "x"), Task("a", "dup")])


def test_dependent_waits_and_failure_blocks():
    graph = TaskGraph([Task("a", "x"), Task("b", "y", dependencies=["a"])])
    out = Scheduler(graph, _ok).run()
    assert out == {"a": "succeeded", "b": "succeeded"}

    graph2 = TaskGraph([Task("a", "x"), Task("b", "y", dependencies=["a"])])
    out2 = Scheduler(graph2, _fail).run()
    assert out2["a"] == "failed" and out2["b"] == "blocked"


def test_allow_on_failure_diagnostic_runs():
    graph = TaskGraph([Task("a", "x"),
                       Task("diag", "diagnose", dependencies=["a"], allow_on_failure=True)])
    out = Scheduler(graph, _fail).run()
    assert out["diag"] == "failed"  # ran (and failed itself); not blocked


def test_retry_budget_enforced(tmp_path):
    store = RunStore(tmp_path / "runs.jsonl")
    graph = TaskGraph([Task("a", "x", max_retries=2)])
    out = Scheduler(graph, _fail, store=store).run()
    assert out["a"] == "failed"
    assert graph.tasks["a"].attempts == 3  # first try + 2 retries
    loaded = RunStore(tmp_path / "runs.jsonl").load()
    assert len(loaded) == 3


def test_journal_replay_is_side_effect_free(tmp_path):
    store = RunStore(tmp_path / "runs.jsonl")
    store.append(RunRecord(run_id="r1", task_id="a", status="running"))
    store.append(RunRecord(run_id="r1", task_id="a", status="succeeded"))
    loaded = RunStore(tmp_path / "runs.jsonl").load()
    assert loaded["r1"].status == "succeeded"
    assert RunStore(tmp_path / "runs.jsonl").load()["r1"].status == "succeeded"


def test_failure_classification_and_strategy_guard():
    assert classify(cancelled=True) == "cancelled"
    assert classify(timed_out=True) == "timeout"
    assert classify(exit_code=1, stderr_tail="Permission denied") == "permission_denied"
    assert classify(exit_code=1) == "implementation_defect"
    assert classify(exit_code=0) == ""
    assert changed_variables({"runtime_selection": "a"}, {"runtime_selection": "b"}) == ["runtime_selection"]
    assert changed_variables({"permission_mode": "x"}, {"permission_mode": "y"}) == []
