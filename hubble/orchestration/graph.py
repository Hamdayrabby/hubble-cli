"""Task DAG: typed nodes, dependency validation, capability requirements.

Validation rejects duplicate IDs, missing dependencies, cycles, and tasks
whose required capabilities no supplied runtime set can satisfy. Retry is a
bounded transition on a node, never a cycle in the graph.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Set

TASK_TYPES = ("audit", "analysis", "implementation", "review", "integration",
              "test", "diagnosis", "documentation")
TASK_STATUSES = ("pending", "ready", "running", "blocked", "succeeded",
                 "failed", "cancelled")


class GraphError(ValueError):
    pass


@dataclass
class Task:
    task_id: str
    objective: str
    task_type: str = "implementation"
    dependencies: List[str] = field(default_factory=list)
    required_capabilities: List[str] = field(default_factory=list)
    owned_paths: List[str] = field(default_factory=list)
    acceptance: List[str] = field(default_factory=list)
    runtime_id: str = ""
    max_retries: int = 2
    allow_on_failure: bool = False
    status: str = "pending"
    attempts: int = 0

    def __post_init__(self):
        if self.task_type not in TASK_TYPES:
            raise GraphError(f"task {self.task_id}: unknown type {self.task_type}")
        if self.max_retries < 0:
            raise GraphError(f"task {self.task_id}: max_retries must be >= 0")


class TaskGraph:
    def __init__(self, tasks: List[Task]):
        self.tasks: Dict[str, Task] = {}
        for task in tasks:
            if task.task_id in self.tasks:
                raise GraphError(f"duplicate task id: {task.task_id}")
            self.tasks[task.task_id] = task
        self.validate()

    def validate(self) -> None:
        for task in self.tasks.values():
            for dep in task.dependencies:
                if dep not in self.tasks:
                    raise GraphError(f"task {task.task_id}: missing dependency {dep}")
        visiting: Set[str] = set()
        done: Set[str] = set()

        def walk(node: str, trail: List[str]):
            if node in done:
                return
            if node in visiting:
                raise GraphError("dependency cycle: " + " -> ".join(trail + [node]))
            visiting.add(node)
            for dep in self.tasks[node].dependencies:
                walk(dep, trail + [node])
            visiting.discard(node)
            done.add(node)

        for task_id in self.tasks:
            walk(task_id, [])

    def dependents(self, task_id: str) -> List[str]:
        return [t.task_id for t in self.tasks.values() if task_id in t.dependencies]

    def ready(self) -> List[Task]:
        out = []
        for task in self.tasks.values():
            if task.status not in ("pending", "ready"):
                continue
            deps_ok = all(
                self.tasks[d].status == "succeeded"
                or (self.tasks[d].status == "failed" and task.allow_on_failure)
                for d in task.dependencies)
            if deps_ok:
                task.status = "ready"
                out.append(task)
        return out

    def refresh_blocked(self) -> None:
        for task in self.tasks.values():
            if task.status in ("pending", "ready"):
                failed_dep = any(self.tasks[d].status in ("failed", "cancelled")
                                  and not task.allow_on_failure
                                  for d in task.dependencies)
                task.status = "blocked" if failed_dep else task.status
