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


# Statuses that say "try again later", not "this model doesn't work": rate limits, overload,
# gateway hiccups. A model that fails with one of these is unknown, never unavailable.
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529}
# Worth another try, but a verdict if it keeps happening: gateways that rotate upstream keys
# return a spurious 401 now and then for a model that works on the next request.
RETRY_STATUS = TRANSIENT_STATUS | {401}


def _probe(client: httpx.Client, base_url: str, headers: Dict[str, str], model: str, timeout: float) -> Dict[str, Any]:
    # 32 tokens, not 10: reasoning models spend a small budget thinking and return empty content.
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 32, "temperature": 0.1}
    start = time.time()
    result = {"model": model, "available": False, "transient": False, "status_code": None, "latency_ms": 0,
              "sample": "", "reason": ""}
    try:
        resp = client.post(f"{base_url}/chat/completions", headers=headers, json=payload, timeout=timeout)
        result["latency_ms"] = round((time.time() - start) * 1000)
        result["status_code"] = resp.status_code
        text = resp.text
        if resp.status_code != 200:
            result["reason"] = f"HTTP {resp.status_code}"
            result["transient"] = resp.status_code in RETRY_STATUS
        elif "upstream returned 403" in text or '"upstream_error"' in text or "unhandled err" in text:
            result["reason"] = "upstream error inside 200"
        else:
            choices = resp.json().get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
            finish = choices[0].get("finish_reason") if choices else None
            if content:
                result.update(available=True, reason="OK", sample=content.replace("\n", " ")[:60])
            elif reasoning or finish == "length":
                # It answered -- it just spent the budget thinking. That model works.
                result.update(available=True, reason="OK (reasoning only)", sample=reasoning.replace("\n", " ")[:60])
            else:
                result["reason"] = "empty content"
    except (httpx.TimeoutException, httpx.TransportError) as e:
        result["latency_ms"] = round((time.time() - start) * 1000)
        result["reason"] = f"{type(e).__name__}"
        result["transient"] = True
    except (httpx.HTTPError, ValueError) as e:
        result["latency_ms"] = round((time.time() - start) * 1000)
        result["reason"] = f"{type(e).__name__}"
    return result


def _previously_working(path: Path) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {m["model"]: m for m in data.get("working_models", []) if m.get("model")}
    except (OSError, ValueError):
        return {}


class ModelScanner:
    """Runs one scan at a time in a daemon thread; `status` is safe to read from the UI."""

    def __init__(self, base_url: str, api_key: str, concurrency: int = 6, timeout: float = 25.0,
                 output: Path = SCAN_FILE, retry_delays=(2.0, 6.0), kind: str = "openai"):
        self.kind = kind
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key
        self.concurrency = concurrency
        self.timeout = timeout
        self.output = output
        self.retry_delays = retry_delays
        self.status = "idle"      # idle | running | done | failed
        self.done = 0
        self.total = 0
        self.working = 0
        self.retry_total = 0      # second phase: rate-limited/timed-out models re-probed one by one
        self.retry_done = 0
        self.error = ""
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return False
        self.status, self.done, self.total, self.working, self.error = "running", 0, 0, 0, ""
        self.retry_total = self.retry_done = 0
        self._thread = threading.Thread(target=self._run, name="hubble-model-scan", daemon=True)
        self._thread.start()
        return True

    def summary(self) -> str:
        if self.status == "running":
            if self.retry_total:
                return f"rechecking busy models {self.retry_done}/{self.retry_total} ({self.working} ok)"
            return f"scanning models {self.done}/{self.total or '?'} ({self.working} ok)"
        if self.status == "done":
            return f"{self.working} models available"
        if self.status == "failed":
            return "model scan failed"
        return ""

    def _run_anthropic(self):
        """Claude's /v1/models lists exactly the models this key can use, with their real
        context size, so there is nothing to probe (and no per-model request to pay for)."""
        from hubble.anthropic_provider import AnthropicProvider
        from hubble.provider import ProviderError
        try:
            models = AnthropicProvider(self.base_url, self.api_key, max_retries=2).list_models()
        except ProviderError as e:
            self.status, self.error = "failed", str(e)[:200]
            return
        self.total = self.done = self.working = len(models)
        if not models:
            self.status, self.error = "failed", "the key has access to no models; kept the previous list"
            return
        save = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "base_url": self.base_url,
                "total_tested": len(models), "all_ids": [m["id"] for m in models], "working_count": len(models),
                "working_models": [{"model": m["id"], "latency_ms": None, "owner": "anthropic", "sample": "",
                                    "context_length": m["context_length"]} for m in models],
                "all_results": [{"model": m["id"], "available": True, "transient": False, "reason": "listed"}
                                for m in models]}
        self.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.output.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(save, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.output)
        self.status = "done"

    def _run(self):
        if self.kind == "anthropic":
            return self._run_anthropic()
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

                # Rate limits/timeouts in a burst of parallel probes are often self-inflicted (many
                # models share one upstream quota). Retry those one at a time, with a pause, before
                # drawing any conclusion.
                for delay in self.retry_delays:
                    pending = [i for i, r in enumerate(results) if r["transient"]]
                    if not pending:
                        break
                    self.retry_total, self.retry_done = len(pending), 0
                    time.sleep(delay)
                    for i in pending:
                        r = _probe(client, self.base_url, headers, results[i]["model"], self.timeout)
                        self.retry_done += 1
                        if r["available"]:
                            self.working += 1
                        results[i] = r
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            self.status, self.error = "failed", f"{type(e).__name__}: {e}"[:200]
            return

        # Still transient after retries: unknown, not unavailable. If the model worked last
        # time, keep it listed as working (flagged) rather than hiding a model that is fine.
        previous = _previously_working(self.output)
        for r in results:
            if r["transient"] and r["status_code"] not in RETRY_STATUS - TRANSIENT_STATUS:
                r["available"] = None
                prev = previous.get(r["model"])
                if prev:
                    r["available"] = True
                    r["stale"] = True
                    r["latency_ms"] = prev.get("latency_ms", r["latency_ms"])
                    r["sample"] = prev.get("sample", "")

        working = sorted((r for r in results if r["available"]), key=lambda r: r["latency_ms"])
        self.working = len(working)
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
                                "context_length": context_lengths.get(r["model"]),
                                **({"stale": True} if r.get("stale") else {})} for r in working],
            "all_results": results,
        }
        self.output.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.output.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(save, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.output)
        self.status = "done"
