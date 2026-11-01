"""Coordinator: bind graph tasks to runtimes, worktrees, and verification.

One writer task = one worktree pinned to the recorded base SHA, the harness
runs with that worktree as cwd, changed files are collected afterwards, and
acceptance commands run independently (a harness exit code of 0 proves
nothing). Integration/merging stays a separate human-gated step.
"""

import subprocess
import time
from pathlib import Path
from typing import List, Optional

from hubble import worktree
from hubble.evaluation.failures import classify
from hubble.orchestration.graph import Task
from hubble.runtimes.protocol import RunSpec
from hubble.runtimes.registry import RuntimeRegistry
from hubble.runs.store import RunRecord

WRITER_TYPES = ("implementation",)


def base_sha(repo: Path) -> str:
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True)
    return proc.stdout.strip()


def run_acceptance(commands: List[List[str]], cwd: Path, timeout_s: float = 300.0) -> bool:
    for argv in commands:
        if not argv:
            continue
        try:
            proc = subprocess.run(argv, cwd=str(cwd), capture_output=True,
                                  text=True, timeout=timeout_s)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if proc.returncode != 0:
            return False
    return True


class Coordinator:
    def __init__(self, repo: Path, registry: RuntimeRegistry,
                 acceptance: Optional[dict] = None):
        self.repo = Path(repo)
        self.registry = registry
        self.acceptance = acceptance or {}

    def execute(self, task: Task) -> RunRecord:
        started = time.time()
        run_id = f"{task.task_id}-{int(started * 1000) % 100000}"
        record = RunRecord(run_id=run_id, task_id=task.task_id,
                           runtime_id=task.runtime_id, status="running",
                           started_at=started, base_sha=base_sha(self.repo))
        cwd = self.repo
        wt_name = ""
        try:
            adapter = self.registry.require(task.required_capabilities,
                                            preferred=task.runtime_id or None)
            record.runtime_id = adapter.runtime_id
            if task.task_type in WRITER_TYPES:
                wt_name = f"orch-{task.task_id}-{run_id[-6:]}"[:61]
                info = worktree.create(self.repo, wt_name, base=record.base_sha or "HEAD")
                cwd = Path(info["path"])
                record.worktree_path = str(cwd)
                record.branch = info["branch"]
            spec = RunSpec(prompt=task.objective, cwd=cwd, task_id=task.task_id,
                           run_id=run_id)
            handle = adapter.start(spec)
            exit_code = 0
            tail: List[str] = []
            event_count = 0
            try:
                for event in adapter.stream(handle):
                    event_count += 1
                    if event.event_type == "failed":
                        exit_code = int(event.payload.get("exit_code") or 1)
                    elif event.event_type == "cancelled":
                        record.status = "cancelled"
                    text = str(event.payload.get("text") or event.payload.get("error")
                               or event.payload.get("raw") or "")
                    if text:
                        tail.append(f"[{event.event_type}] {text[:500]}")
                        tail = tail[-20:]
            finally:
                adapter.close(handle)
            record.detail = {"events": event_count, "tail": tail}
            record.exit_code = exit_code
            if record.status != "cancelled":
                checks = [cmd.split() for cmd in (task.acceptance or self.acceptance.get(task.task_id, []))]
                verified = run_acceptance(checks, cwd) if checks else exit_code == 0
                record.status = "succeeded" if (exit_code == 0 and verified) else "failed"
                if record.status == "failed":
                    record.failure_class = classify(exit_code=exit_code) or "test_failure"
            if wt_name:
                status = worktree.status(cwd)
                record.changed_files = [ln[3:] for ln in status["dirty"].splitlines() if ln.strip()]
                record.detail.setdefault("porcelain", status["dirty"][:4000])
                record.detail.setdefault("diffstat", status["diffstat"][:2000])
        except Exception as exc:  # adapter/registry/worktree errors stay visible
            record.status = "failed"
            record.failure_class = classify(stderr_tail=str(exc)) or "adapter_error"
            record.detail = {"error": str(exc)[:500]}
        record.ended_at = time.time()
        return record
