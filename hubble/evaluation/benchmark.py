"""Benchmark comparison: keep a strategy only on measured improvement.

A strategy is a named mapping of the variables in evaluation/failures.py.
`compare` runs the same fixed task set under two strategies and promotes the
challenger only when its target metric improves without breaching the
regression guardrails. No improvement means no promotion; regressions block.
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List

TaskRunner = Callable[[str, Dict[str, str]], Dict[str, float]]


@dataclass
class BenchmarkResult:
    strategy: str
    acceptance_rate: float = 0.0
    regressions: int = 0
    duration_s: float = 0.0
    retries: int = 0

    @property
    def promotable(self) -> bool:
        return self.regressions == 0


def compare(baseline: BenchmarkResult, challenger: BenchmarkResult,
            metric: str = "acceptance_rate") -> Dict[str, object]:
    base = getattr(baseline, metric)
    new = getattr(challenger, metric)
    improved = new > base
    promoted = bool(improved and challenger.promotable)
    return {"metric": metric, "baseline": base, "challenger": new,
            "improved": improved, "promoted": promoted,
            "reason": ("promoted" if promoted
                       else "no improvement" if not improved
                       else f"blocked by {challenger.regressions} regression(s)")}


def run_benchmark(tasks: List[str], strategy: Dict[str, str],
                  execute: TaskRunner) -> BenchmarkResult:
    """Execute a fixed task set under one strategy and score it."""
    wins = 0
    retries = 0
    regressions = 0
    for task_id in tasks:
        outcome = execute(task_id, strategy)
        wins += 1 if outcome.get("accepted", 0.0) >= 1.0 else 0
        retries += int(outcome.get("retries", 0))
        regressions += int(outcome.get("regressions", 0))
    return BenchmarkResult(strategy=strategy.get("name", "unnamed"),
                           acceptance_rate=wins / len(tasks) if tasks else 0.0,
                           regressions=regressions, retries=retries)
