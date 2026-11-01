"""Operator entry point for cross-runtime orchestration.

Usage:
  python -m hubble.orchestrate --list-runtimes
  python -m hubble.orchestrate --repo . --task impl:"fix the test" --runtime codex

State lands in <repo>/.hubble/runs.jsonl (append-only journal). Worktree
merging is never automatic: inspect branches hubble/orch-* and merge by hand.
"""

import argparse
import json
import sys
from pathlib import Path

from hubble.orchestration.coordinator import Coordinator
from hubble.orchestration.graph import Task, TaskGraph
from hubble.orchestration.scheduler import Scheduler
from hubble.runtimes import default_registry
from hubble.runs.store import RunStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hubble-orchestrate")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--list-runtimes", action="store_true")
    parser.add_argument("--task", action="append", default=[],
                        help="ID:objective (repeatable; later tasks depend on earlier ones)")
    parser.add_argument("--runtime", default="")
    parser.add_argument("--concurrency", type=int, default=3)
    args = parser.parse_args(argv)

    registry = default_registry(include_fake=True)
    if args.list_runtimes:
        rows = [{"runtime": d.runtime_id, "available": d.available,
                 "version": d.version, "diagnostics": d.diagnostics}
                for d in registry.detect_all()]
        print(json.dumps(rows, indent=2))
        return 0

    tasks = []
    previous = ""
    for raw in args.task:
        task_id, _, objective = raw.partition(":")
        if not objective:
            parser.error(f"--task expects ID:objective, got {raw!r}")
        tasks.append(Task(task_id=task_id.strip(),
                          objective=objective.strip(),
                          dependencies=[previous] if previous else [],
                          runtime_id=args.runtime,
                          required_capabilities=["json_stream"]))
        previous = task_id.strip()
    if not tasks:
        parser.error("nothing to do: pass --task or --list-runtimes")

    repo = Path(args.repo).resolve()
    coordinator = Coordinator(repo, registry)
    store = RunStore(repo / ".hubble" / "runs.jsonl")
    result = Scheduler(TaskGraph(tasks), coordinator.execute, store=store,
                       max_concurrency=max(1, args.concurrency)).run()
    print(json.dumps(result, indent=2))
    return 0 if all(s == "succeeded" for s in result.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
