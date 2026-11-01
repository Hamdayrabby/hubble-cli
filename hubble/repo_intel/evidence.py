"""Repository Intelligence: evidence-backed source analysis (Hubble's own).

Every claim carries a repository-relative path, line span, evidence type and
confidence. Dynamic/unresolved links are labelled, never invented. This is
not REA; REA stays an optional external MCP specialist.
"""

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

EVIDENCE_TYPES = ("ast", "import", "test", "runtime_trace", "search", "agent_inference")


@dataclass
class Evidence:
    path: str
    start_line: int = 0
    end_line: int = 0
    symbol: str = ""
    evidence_type: str = "ast"
    confidence: str = "high"  # high | medium | low
    note: str = ""

    def __post_init__(self):
        if self.evidence_type not in EVIDENCE_TYPES:
            raise ValueError(f"unknown evidence type: {self.evidence_type}")
        if self.confidence not in ("high", "medium", "low"):
            raise ValueError(f"unknown confidence: {self.confidence}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ImportEdge:
    source: str
    target: str
    kind: str  # local | external | unresolved
    evidence: Optional[Evidence] = None

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["evidence"] = self.evidence.to_dict() if self.evidence else None
        return out
