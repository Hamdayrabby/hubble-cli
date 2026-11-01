"""Reproducible orchestration demo (offline, no keys, no cost).

Builds a scratch repo, runs one fake-runtime task through the coordinator,
and prints the journal plus worktree state. Verifies the pipeline, not a
harness: swap --runtime for a real one to exercise an actual CLI.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


def main() -> int:
    from hubble.orchestration.coordinator import Coordinator
    from hubble.orchestration.graph import Task, TaskGraph
    from hubble.orchestration.scheduler import Scheduler
    from hubble.runtimes.adapters.fake import FakeAdapter
    from hubble.runtimes.registry import RuntimeRegistry
    from hubble.runs.store import RunStore

    with tempfile.TemporaryDirectory(prefix="hubble-orch-demo-") as tmp:
        root = Path(tmp)
        git(root, "init", "-q", "-b", "main")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t",
            "config", "user.email", "t@t")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t",
            "config", "user.name", "t")
        (root / "app.py").write_text("x = 1\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t",
            "commit", "-qm", "init")

        registry = RuntimeRegistry()
        registry.register(FakeAdapter(script=["demo: working", "demo: done"]))
        coordinator = Coordinator(root, registry)
        store = RunStore(root / ".hubble" / "runs.jsonl")
        graph = TaskGraph([Task(task_id="demo", objective="add a hello function",
                                required_capabilities=["one_shot"])])
        result = Scheduler(graph, coordinator.execute, store=store).run()
        print(json.dumps(result, indent=2))
        record = store.load()
        last = next(iter(record.values()))
        print(f"branch={last.branch} base={last.base_sha[:8]} "
              f"exit={last.exit_code} events={last.detail.get('events')}")
        if result != {"demo": "succeeded"}:
            return 1
    print("demo ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
