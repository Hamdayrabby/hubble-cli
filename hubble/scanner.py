"""Background model health scan: fetch /models, send a tiny completion to each, save working ones.

Writes available_models.json in the same format as test_models.py, so both stay compatible.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from hubble.models import SCAN_FILE
from hubble.provider import normalize_base_url


def scan_age_hours(path: Path = SCAN_FILE) -> Optional[float]:
    try:
        return (time.time() - path.stat().st_mtime) / 3600
    except OSError:
        return None


def _context_length(entry: Dict[str, Any]) -> Optional[int]:
    """Pull a model's context size out of a /models entry, if the gateway publishes one.
    Field name varies by provider: OpenRouter uses top-level `context_length` (and repeats it
    under `top_provider`); a few others use `context_window`. Most gateways publish neither."""
    for key in ("context_length", "context_window"):
        val = entry.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    top = entry.get("top_provider")
    if isinstance(top, dict):
        val = top.get("context_length")
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return None


def _probe(client: httpx.Client, base_url: str, headers: Dict[str, str], model: str, timeout: float) -> Dict[str, Any]:
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10, "temperature": 0.1}
    start = time.time()
    result = {"model": model, "available": False, "status_code": None, "latency_ms": 0, "sample": "", "reason": ""}
    try:
        resp = client.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=timeout)
        result["latency_ms"] = round((time.time() - start) * 1000)
        result["status_code"] = resp.status_code
        text = resp.text
        if resp.status_code != 200:
            result["reason"] = f"HTTP {resp.status_code}"
        elif "upstream returned 403" in text or '"upstream_error"' in text or "unhandled err" in text:
            result["reason"] = "upstream error inside 200"
        else:
            choices = resp.json().get("choices") or []
            content = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
            if content:
                result.update(available=True, reason="OK", sample=content.replace("\n", " ")[:60])
            else:
                result["reason"] = "empty content"
    except (httpx.HTTPError, ValueError) as e:
        result["latency_ms"] = round((time.time() - start) * 1000)
        result["reason"] = f"{type(e).__name__}"
    return result


class ModelScanner:
    """Runs one scan at a time in a daemon thread; `status` is safe to read from the UI."""

    def __init__(self, base_url: str, api_key: str, concurrency: int = 12, timeout: float = 12.0,
                 output: Path = SCAN_FILE):
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key
        self.concurrency = concurrency
        self.timeout = timeout
        self.output = output
        self.status = "idle"      # idle | running | done | failed
        self.done = 0
        self.total = 0
        self.working = 0
        self.error = ""
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return False
        self.status, self.done, self.total, self.working, self.error = "running", 0, 0, 0, ""
        self._thread = threading.Thread(target=self._run, name="hubble-model-scan", daemon=True)
        self._thread.start()
        return True

    def summary(self) -> str:
        if self.status == "running":
            return f"scanning models {self.done}/{self.total or '?'}"
        if self.status == "done":
            return f"{self.working} models available"
        if self.status == "failed":
            return "model scan failed"
        return ""

    def _run(self):
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout + 5) as client:
                resp = client.get(f"{self.base_url}/models", headers=headers)
                resp.raise_for_status()
                data = resp.json()
                entries = data.get("data", data) if isinstance(data, dict) else data
                owners = {m["id"]: m.get("owned_by", "") for m in entries if isinstance(m, dict) and m.get("id")}
                # Some gateways (OpenRouter and a few others) publish each model's real context
                # size; most (including the default AIHub gateway) do not, so this is best-effort.
                context_lengths = {m["id"]: _context_length(m) for m in entries
                                   if isinstance(m, dict) and m.get("id")}
                self.total = len(owners)
                results: List[Dict[str, Any]] = []

                def task(name):
                    r = _probe(client, self.base_url, headers, name, self.timeout)
                    self.done += 1
                    if r["available"]:
                        self.working += 1
                    return r

                with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                    results = list(pool.map(task, owners))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            self.status, self.error = "failed", f"{type(e).__name__}: {e}"[:200]
            return

        working = sorted((r for r in results if r["available"]), key=lambda r: r["latency_ms"])
        if not working:
            # A gateway outage or bad key would otherwise wipe the model list.
            self.status, self.error = "failed", "no model responded; kept the previous list"
            return
        save = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "base_url": self.base_url,
            "total_tested": len(results),
            "all_ids": list(owners),
            "working_count": len(working),
            "working_models": [{"model": r["model"], "latency_ms": r["latency_ms"],
                                "owner": owners.get(r["model"], ""), "sample": r["sample"],
                                "context_length": context_lengths.get(r["model"])} for r in working],
            "all_results": results,
        }
        self.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.output.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(save, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.output)
        self.status = "done"
