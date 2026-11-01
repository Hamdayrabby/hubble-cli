"""Persistent run journal: append-only JSONL, idempotent replay.

Deliberately separate from hubble/session.py (conversation transcripts).
Replaying the journal rebuilds state only; it never re-launches a process,
re-merges a branch, or repeats a side effect.
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class RunRecord:
    run_id: str
    task_id: str = ""
    runtime_id: str = ""
    status: str = "pending"
    attempt: int = 0
    base_sha: str = ""
    worktree_path: str = ""
    branch: str = ""
    exit_code: Optional[int] = None
    failure_class: str = ""
    strategy_version: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0
    changed_files: List[str] = field(default_factory=list)
    detail: Dict[str, Any] = field(default_factory=dict)


class RunStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.records: Dict[str, RunRecord] = {}

    def append(self, record: RunRecord) -> None:
        self.records[record.run_id] = record
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": time.time(), "record": asdict(record)}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def load(self) -> Dict[str, RunRecord]:
        self.records = {}
        if not self.path.is_file():
            return self.records
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line).get("record") or {}
                    record = RunRecord(**{k: v for k, v in rec.items()
                                          if k in RunRecord.__dataclass_fields__})
                except (ValueError, TypeError):
                    continue  # torn tail write; earlier records stay valid
                self.records[record.run_id] = record  # last write wins
        return self.records

    def reconcile(self) -> List[RunRecord]:
        """Runs left in 'running' by an interruption need human resolution."""
        return [r for r in self.records.values() if r.status == "running"]
