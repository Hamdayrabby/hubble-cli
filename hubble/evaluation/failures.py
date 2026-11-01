"""Failure taxonomy for the bounded evaluation loop.

A retry may change one strategy variable per attempt; security policy,
permission modes, sandbox constraints and approval gates are never strategy
variables and must never appear in a strategy change.
"""

from typing import Dict, List

FAILURE_CLASSES = (
    "runtime_unavailable", "adapter_error", "timeout", "cancelled",
    "planning_error", "missing_context", "implementation_defect",
    "test_failure", "worktree_conflict", "permission_denied", "budget_exhausted",
)

# The only knobs the loop may turn, one at a time.
STRATEGY_VARIABLES = (
    "runtime_selection", "task_granularity", "agent_assignment",
    "prompt_template", "context_selection", "verification_order",
    "timeout", "concurrency",
)


def classify(exit_code: int = 0, timed_out: bool = False, cancelled: bool = False,
             stderr_tail: str = "") -> str:
    if cancelled:
        return "cancelled"
    if timed_out:
        return "timeout"
    text = (stderr_tail or "").lower()
    if "permission" in text or "denied" in text or "sandbox" in text:
        return "permission_denied"
    if "not found" in text and ("executable" in text or "cli" in text):
        return "runtime_unavailable"
    if "conflict" in text:
        return "worktree_conflict"
    if exit_code != 0:
        return "implementation_defect"
    return ""


def changed_variables(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    keys = [k for k in STRATEGY_VARIABLES if before.get(k) != after.get(k)]
    return keys
