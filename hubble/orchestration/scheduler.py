"""Bounded scheduler: ready-queue execution with failure propagation.

Independent tasks run in parallel up to ``max_concurrency``; writers must be
given isolated worktrees by the caller (a shared checkout is never chosen
silently here). A failed prerequisite blocks dependents unless the dependent
is an allowed-on-failure diagnostic. Retries are bounded per task.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional

from hubble.orchestration.graph import Task, TaskGraph
from hubble.runs.store import RunRecord, RunStore

FAILURE_CLASSES = (
    "runtime_unavailable", "adapter_error", "timeout", "cancelled",
    "planning_error", "missing_context", "implementation_defect",
    "test_failure", "worktree_conflict", "permission_denied", "budget_exhausted",
)

Executor = Callable[[Task], RunRecord]


class Scheduler:
    def __init__(self, graph: TaskGraph, execute: Executor,
                 store: Optional[RunStore] = None, max_concurrency: int = 3,
                 strategy_version: str = "v1"):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.graph = graph
        self.execute = execute
        self.store = store
        self.max_concurrency = max_concurrency
        self.strategy_version = strategy_version
        self._cancelled = threading.Event()
        self.history: List[RunRecord] = []

    def cancel(self) -> None:
        self._cancelled.set()

    def _run_task(self, task: Task) -> None:
        while True:
            task.status = "running"
            task.attempts += 1
            record = self.execute(task)
            record.strategy_version = record.strategy_version or self.strategy_version
            record.attempt = task.attempts
            self.history.append(record)
            if self.store:
                self.store.append(record)
            if record.status == "succeeded":
                task.status = "succeeded"
                return
            if self._cancelled.is_set():
                task.status = "cancelled"
                return
            if task.attempts > task.max_retries:
                task.status = "failed"
                return

    def resume_from(self, store: "RunStore") -> List[str]:
        """Restore task state from a persisted journal.

        Succeeded tasks stay done (never re-run). Failed tasks return to
        pending within their remaining retry budget. Runs left 'running' by
        an interruption are ambiguous side effects: they come back as
        pending too, but their run ids are returned so the operator can
        inspect worktrees before re-execution.
        """
        records = store.load()
        ambiguous: List[str] = []
        latest: Dict[str, object] = {}
        for record in records.values():
            key = record.task_id
            prev = latest.get(key)
            if prev is None or record.attempt >= prev.attempt:  # type: ignore[union-attr]
                latest[key] = record
        for task_id, record in latest.items():  # type: ignore[union-attr]
            task = self.graph.tasks.get(task_id)
            if task is None:
                continue
            status = record.status  # type: ignore[union-attr]
            if status == "succeeded":
                task.status = "succeeded"
            elif status == "failed":
                task.status = "pending"
                task.attempts = record.attempt  # type: ignore[union-attr]
            elif status == "running":
                task.status = "pending"
                ambiguous.append(record.run_id)  # type: ignore[union-attr]
        self.graph.refresh_blocked()
        return ambiguous

    def run(self) -> Dict[str, str]:
        with ThreadPoolExecutor(max_workers=self.max_concurrency) as pool:
            futures = {}
            while True:
                self.graph.refresh_blocked()
                for task in self.graph.ready():
                    if task.task_id not in futures and not self._cancelled.is_set():
                        task.status = "running"
                        futures[task.task_id] = pool.submit(self._run_task, task)
                if not futures:
                    break
                done = [tid for tid, f in futures.items() if f.done()]
                for tid in done:
                    futures.pop(tid).result()
                if not done:
                    time.sleep(0.01)
                if all(t.status in ("succeeded", "failed", "cancelled", "blocked")
                       for t in self.graph.tasks.values()):
                    break
        self.graph.refresh_blocked()
        return {tid: t.status for tid, t in self.graph.tasks.items()}
