"""Normalized runtime events: one versioned envelope for every external harness.

Every event a runtime adapter emits uses this envelope so the orchestrator never
parses harness-specific JSONL itself. Raw harness output, when kept at all, lives
bounded inside ``payload["raw"]`` and must never contain secrets.
"""

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional

SCHEMA_VERSION = 1

EVENT_TYPES = (
    "process_started", "stdout", "stderr", "status", "approval_required",
    "usage", "warning", "completed", "failed", "cancelled", "parse_error",
)


@dataclass
class RuntimeEvent:
    event_type: str
    task_id: str = ""
    run_id: str = ""
    runtime_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    timestamp: float = 0.0
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RuntimeEvent":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class EventSequencer:
    """Assigns monotonic per-run sequence numbers and timestamps."""

    def __init__(self, task_id: str = "", run_id: str = "", runtime_id: str = ""):
        self.task_id = task_id
        self.run_id = run_id
        self.runtime_id = runtime_id
        self._next = 0

    def emit(self, event_type: str, payload: Optional[Dict[str, Any]] = None,
             raw: Optional[str] = None, raw_limit: int = 2000) -> RuntimeEvent:
        self._next += 1
        body: Dict[str, Any] = dict(payload or {})
        if raw is not None:
            body["raw"] = raw[:raw_limit]
        return RuntimeEvent(event_type=event_type, task_id=self.task_id,
                            run_id=self.run_id, runtime_id=self.runtime_id,
                            payload=body, sequence=self._next, timestamp=time.time())

    def wrap(self, events: Iterator[RuntimeEvent]) -> Iterator[RuntimeEvent]:
        for ev in events:
            self._next += 1
            ev.sequence = self._next
            if not ev.timestamp:
                ev.timestamp = time.time()
            ev.task_id = ev.task_id or self.task_id
            ev.run_id = ev.run_id or self.run_id
            ev.runtime_id = ev.runtime_id or self.runtime_id
            yield ev


def bounded_tail(lines: List[str], limit: int = 4000) -> str:
    text = "".join(lines)
    return text[-limit:] if len(text) > limit else text
