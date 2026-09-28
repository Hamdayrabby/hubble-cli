"""Usage stats: which models actually finish your tasks, how fast, and at what cost.

Every model call and every task (one prompt, start to final answer) is appended as one JSON
line to ~/.hubble/stats.jsonl. Nothing leaves the machine. `/stats` or `hubble --stats`
summarizes it per model; `"stats": false` in settings turns recording off.

Costs are estimates: known list prices per million tokens (settings `model_prices` overrides
or adds, e.g. {"my-model": [0.5, 1.5]}); models with no known price show "-", and models on free
gateways cost nothing to you anyway.
"""

import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from hubble.settings import HOME_DIR

STATS_FILE = HOME_DIR / "stats.jsonl"
MAX_BYTES = 20 * 1024 * 1024  # rotate to stats.1.jsonl beyond this

# USD per million tokens (input, output). Claude list prices; extend with settings.model_prices.
PRICES: Dict[str, Tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0), "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0), "claude-opus-4-7": (5.0, 25.0), "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-4-6": (3.0, 15.0), "claude-haiku-4-5": (1.0, 5.0),
}


class StatsLog:
    def __init__(self, path: Optional[Path] = None, enabled: bool = True):
        self.path = path or STATS_FILE
        self.enabled = enabled
        self._lock = threading.Lock()

    def _write(self, record: Dict[str, Any]):
        if not self.enabled:
            return
        record.setdefault("ts", time.time())
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    self.path.replace(self.path.with_name(self.path.stem + ".1.jsonl"))
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            pass  # stats must never break a session

    def call(self, provider: str, model: str, ok: bool, *, status: Optional[int] = None, error: str = "",
             prompt_tokens: int = 0, completion_tokens: int = 0, duration: float = 0.0, ttft_ms: int = 0,
             subagent: bool = False, fallback: bool = False):
        self._write({"type": "call", "provider": provider, "model": model, "ok": ok, "status": status,
                     "error": error[:120], "in": prompt_tokens, "out": completion_tokens,
                     "dur": round(duration, 3), "ttft": ttft_ms, "sub": subagent, "fallback": fallback})

    def task(self, provider: str, model: str, ok: bool, *, reason: str = "", calls: int = 0, tools: int = 0,
             prompt_tokens: int = 0, completion_tokens: int = 0, duration: float = 0.0,
             tier: str = "", escalated: bool = False, subagent: bool = False):
        self._write({"type": "task", "provider": provider, "model": model, "ok": ok, "reason": reason,
                     "calls": calls, "tools": tools, "in": prompt_tokens, "out": completion_tokens,
                     "dur": round(duration, 3), "tier": tier, "escalated": escalated, "sub": subagent})


def load(path: Optional[Path] = None, days: Optional[float] = None) -> List[Dict[str, Any]]:
    path = path or STATS_FILE
    since = time.time() - days * 86400 if days else 0
    out = []
    for p in (path.with_name(path.stem + ".1.jsonl"), path):
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get("ts", 0) >= since:
                        out.append(rec)
        except OSError:
            continue
    return out


def price_for(model: str, overrides: Optional[Dict[str, Any]] = None) -> Optional[Tuple[float, float]]:
    for table in (overrides or {}, PRICES):
        val = table.get(model)
        if isinstance(val, (list, tuple)) and len(val) == 2:
            return float(val[0]), float(val[1])
    return None


def summarize(records: Iterable[Dict[str, Any]], prices: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """One row per (provider, model): calls, errors, tasks finished, speed, tokens, cost."""
    rows: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(lambda: {
        "calls": 0, "ok_calls": 0, "errors": defaultdict(int), "tasks": 0, "tasks_ok": 0, "in": 0, "out": 0,
        "gen_time": 0.0, "ttft": [], "fallback_calls": 0, "escalated": 0})
    for r in records:
        key = (r.get("provider", "?"), r.get("model", "?"))
        row = rows[key]
        if r.get("type") == "call":
            row["calls"] += 1
            row["fallback_calls"] += 1 if r.get("fallback") else 0
            if r.get("ok"):
                row["ok_calls"] += 1
                row["in"] += int(r.get("in") or 0)
                row["out"] += int(r.get("out") or 0)
                row["gen_time"] += float(r.get("dur") or 0)
                if r.get("ttft"):
                    row["ttft"].append(int(r["ttft"]))
            else:
                row["errors"][str(r.get("status") or r.get("error") or "error")] += 1
        elif r.get("type") == "task" and not r.get("sub"):
            row["tasks"] += 1
            row["tasks_ok"] += 1 if r.get("ok") else 0
            row["escalated"] += 1 if r.get("escalated") else 0
    out = []
    for (provider, model), row in rows.items():
        price = price_for(model, prices)
        cost = (row["in"] * price[0] + row["out"] * price[1]) / 1e6 if price else None
        ttfts = sorted(row["ttft"])
        out.append({
            "provider": provider, "model": model, "calls": row["calls"], "ok_calls": row["ok_calls"],
            "tasks_ok_n": row["tasks_ok"],
            "call_success": row["ok_calls"] / row["calls"] if row["calls"] else None,
            "errors": dict(row["errors"]), "tasks": row["tasks"],
            "task_success": row["tasks_ok"] / row["tasks"] if row["tasks"] else None,
            "tokens_in": row["in"], "tokens_out": row["out"],
            "tok_per_s": row["out"] / row["gen_time"] if row["gen_time"] else None,
            "median_ttft_ms": ttfts[len(ttfts) // 2] if ttfts else None,
            "cost": cost, "fallback_calls": row["fallback_calls"], "escalated": row["escalated"],
        })
    out.sort(key=lambda r: (-(r["tasks"] or 0), -r["calls"]))
    return out


def render(rows: List[Dict[str, Any]], days: Optional[float] = None):
    """A rich renderable: the per-model table plus a one-line total."""
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    if not rows:
        return Text("No usage recorded yet" + (f" in the last {days:g} days" if days else "") + ".", style="dim")
    t = Table(box=None, padding=(0, 1), header_style="bold #8787af", pad_edge=False)
    t.add_column("model", overflow="fold", ratio=3)
    for col in ("tasks ok", "calls ok", "errors", "tok/s", "1st tok", "tokens", "cost"):
        t.add_column(col, justify="left" if col == "errors" else "right", no_wrap=col != "errors")

    def pct(v):
        return "-" if v is None else f"{v:.0%}"

    def short(n):
        return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e4 else f"{n:,}"

    total_cost, any_cost = 0.0, False
    for r in rows:
        errs = " ".join(f"{k}×{v}" for k, v in sorted(r["errors"].items(), key=lambda kv: -kv[1])[:3]) or "-"
        tasks = f"{r['tasks_ok_n']}/{r['tasks']} {pct(r['task_success'])}" if r["tasks"] else "-"
        if r["escalated"]:
            tasks += f" ↑{r['escalated']}"
        cost = "-" if r["cost"] is None else f"${r['cost']:.2f}"
        if r["cost"] is not None:
            total_cost, any_cost = total_cost + r["cost"], True
        t.add_row(Text(r["model"], style="bold #00d7ff").append(f" {r['provider']}", style="dim"), tasks,
                  f"{r['ok_calls']}/{r['calls']}", Text(errs, style="yellow" if r["errors"] else "dim"),
                  "-" if r["tok_per_s"] is None else f"{r['tok_per_s']:.1f}",
                  "-" if r["median_ttft_ms"] is None else f"{r['median_ttft_ms'] / 1000:.1f}s",
                  f"{short(r['tokens_in'])}/{short(r['tokens_out'])}", cost)
    period = f"last {days:g} days" if days else "all time"
    tail = Text(f"{period} · {sum(r['tasks'] for r in rows)} tasks · {sum(r['calls'] for r in rows)} model calls"
                + (f" · about ${total_cost:.2f} at list prices" if any_cost else ""), style="dim")
    note = Text("tasks ok = your prompts that ended with an answer (not an error or Ctrl+C) · ↑ = escalated to "
                "the strong model · 1st tok = median time to first token", style="dim")
    return Group(t, tail, note)


def best_fast_model(rows: List[Dict[str, Any]], min_calls: int = 5, min_success: float = 0.9,
                    exclude: Iterable[str] = ()) -> Optional[Dict[str, Any]]:
    """Fastest reliable model you have actually used: the natural 'fast' tier for routing."""
    excluded = set(exclude)
    good = [r for r in rows if r["calls"] >= min_calls and (r["call_success"] or 0) >= min_success
            and r["tok_per_s"] and f"{r['provider']}:{r['model']}" not in excluded and r["model"] not in excluded]
    return max(good, key=lambda r: r["tok_per_s"], default=None)
