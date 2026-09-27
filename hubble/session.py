"""Append-only JSONL session transcripts under ~/.hubble/projects/<project>/<id>.jsonl.

Record types: meta (first line), msg (one chat message), reset (history replaced by compaction).
"""

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hubble.settings import HOME_DIR


def project_dir(root: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", root.name).strip("-")[:40] or "root"
    digest = hashlib.sha1(str(root).lower().encode()).hexdigest()[:8]
    return HOME_DIR / "projects" / f"{slug}-{digest}"


class Session:
    def __init__(self, path: Path, session_id: str):
        self.path = path
        self.id = session_id

    def _write(self, record: Dict[str, Any]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", time.time())
        line = json.dumps(record, ensure_ascii=False) + "\n"
        if self.path.exists() and self.path.stat().st_size:
            with open(self.path, "rb") as f:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    line = "\n" + line  # previous write was cut off; start a fresh line
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)

    def append(self, message: Dict[str, Any]):
        self._write({"type": "msg", "message": message})

    def reset(self, messages: List[Dict[str, Any]]):
        self._write({"type": "reset", "messages": messages})

    def meta(self, **fields):
        self._write({"type": "meta", **fields})


class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.dir = project_dir(root)

    def new(self, model: str) -> Session:
        sid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        session = Session(self.dir / f"{sid}.jsonl", sid)
        session.meta(cwd=str(self.root), model=model)
        return session

    def list(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        files = sorted(self.dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for p in files[:limit]:
            messages, meta = self._replay(p)
            from hubble.images import content_text
            first = next((content_text(m.get("content")) for m in messages
                          if m.get("role") == "user" and m.get("content")), "")
            if not messages:
                continue
            out.append({"id": p.stem, "updated": p.stat().st_mtime, "model": meta.get("model"),
                        "messages": len(messages), "title": (first or "").strip().splitlines()[0][:70] if first else ""})
        return out

    def latest(self) -> Optional[str]:
        items = self.list(limit=1)
        return items[0]["id"] if items else None

    def load(self, session_id: str) -> Tuple[Session, List[Dict[str, Any]], Dict[str, Any]]:
        matches = [p for p in self.dir.glob("*.jsonl") if p.stem == session_id] or \
                  [p for p in self.dir.glob("*.jsonl") if p.stem.startswith(session_id) or session_id in p.stem]
        if not matches:
            raise FileNotFoundError(f"No session '{session_id}' for this project")
        p = matches[0]
        messages, meta = self._replay(p)
        return Session(p, p.stem), messages, meta

    @staticmethod
    def _replay(path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        messages: List[Dict[str, Any]] = []
        meta: Dict[str, Any] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue  # a crash can leave a partial last line
                    kind = rec.get("type")
                    if kind == "msg":
                        messages.append(rec["message"])
                    elif kind == "reset":
                        messages = list(rec.get("messages", []))
                    elif kind == "meta":
                        meta.update({k: v for k, v in rec.items() if k not in ("type", "ts")})
        except OSError:
            pass
        return repair_history(messages), meta


def repair_history(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Give every assistant tool call a result, so a transcript cut mid-turn stays sendable."""
    out: List[Dict[str, Any]] = []
    pending: List[str] = []
    for m in messages:
        if pending and m.get("role") != "tool":
            out.extend({"role": "tool", "tool_call_id": cid, "content": "Interrupted: no result recorded."}
                       for cid in pending)
            pending = []
        out.append(m)
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending = [tc["id"] for tc in m["tool_calls"]]
        elif m.get("role") == "tool" and m.get("tool_call_id") in pending:
            pending.remove(m["tool_call_id"])
    out.extend({"role": "tool", "tool_call_id": cid, "content": "Interrupted: no result recorded."}
               for cid in pending)
    return out
