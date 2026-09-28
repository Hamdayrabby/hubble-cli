"""OpenAI-compatible streaming client with native tool calling."""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx
from urllib.parse import urlparse

RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}  # 529: Anthropic "overloaded"


class ProviderError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status

    @property
    def transient(self) -> bool:
        """Rate limits, overload and outages: another model may still work."""
        return self.status in RETRY_STATUS or self.status is None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class TurnResult:
    text: str = ""
    reasoning: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    ttft_ms: int = 0
    duration: float = 0.0
    # Provider-native assistant content (Claude's blocks, thinking signatures included), stored on
    # the history message so the same provider can send it back verbatim next turn.
    raw_content: Optional[List[Dict[str, Any]]] = None


def normalize_base_url(url: str) -> str:
    """Append /v1 only to a bare host; keep explicit paths such as /api/v1 or /v1beta/openai."""
    url = url.strip().rstrip("/")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    if urlparse(url).path in ("", "/"):
        url += "/v1"
    return url


def _short_id(raw: str) -> str:
    # Mistral rejects tool_call ids that are not 9 alphanumeric chars; other backends accept any.
    return hashlib.sha1(raw.encode()).hexdigest()[:9]


def normalize_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rewrite tool_call ids so a history built with one model stays valid for another."""
    out = []
    for m in messages:
        m = {k: v for k, v in m.items() if not k.startswith("_")}  # provider-private keys stay local
        if m.get("tool_calls"):
            m["tool_calls"] = [{**tc, "id": _short_id(tc["id"])} for tc in m["tool_calls"]]
            if not m.get("content"):
                m["content"] = ""
        if m.get("role") == "tool":
            m["tool_call_id"] = _short_id(m["tool_call_id"])
        out.append(m)
    return out


def _error_message(body: str) -> str:
    """Short human message. Gateways often wrap the upstream error as JSON inside the message."""
    try:
        err = json.loads(body).get("error", {})
    except (ValueError, AttributeError):
        return body.strip()[:200]
    if not isinstance(err, dict):
        return str(err)[:200]
    msg = err.get("message") or json.dumps(err)
    inner = re.search(r"\{.*\}", msg, re.DOTALL)
    if inner:
        try:
            raw = json.loads(inner.group(0)).get("raw")
            if raw:
                msg = raw
        except (ValueError, AttributeError):
            pass
    msg = re.split(r"(?<=\.)\s", msg.strip(), maxsplit=1)[0]  # first sentence is the useful part
    return msg[:200]


def _retry_after(resp: httpx.Response) -> Optional[float]:
    try:
        return max(0.0, min(float(resp.headers.get("retry-after", "")), 20.0))
    except ValueError:
        return None


class OpenAICompatProvider:
    def __init__(self, base_url: str, api_key: str, client: Optional[httpx.Client] = None,
                 max_retries: int = 3):
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key
        self.max_retries = max_retries
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=15.0))

    @property
    def headers(self) -> Dict[str, str]:
        # An honest, stable client name, so gateways that allowlist clients can recognize Hubble.
        from hubble import __version__
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                "User-Agent": f"hubble-cli/{__version__} (+https://github.com/Hamdayrabby/hubble-cli)"}

    def stream(self, model: str, messages: List[Dict[str, Any]], tools: Optional[List[Dict]] = None,
               temperature: float = 0.3, max_tokens: int = 8192,
               on_text: Optional[Callable[[str], None]] = None,
               on_reasoning: Optional[Callable[[str], None]] = None) -> TurnResult:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": normalize_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        attempt = 0
        while True:
            attempt += 1
            try:
                return self._stream_once(payload, on_text, on_reasoning)
            except _Retryable as e:
                if attempt > self.max_retries:
                    raise ProviderError(str(e), e.status) from None
                time.sleep(e.wait if e.wait is not None else min(2 ** (attempt - 1), 8))

    def _stream_once(self, payload, on_text, on_reasoning) -> TurnResult:
        result = TurnResult()
        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        calls: Dict[int, Dict[str, str]] = {}
        start = time.time()
        got_delta = False

        try:
            with self.client.stream("POST", f"{self.base_url}/chat/completions",
                                    headers=self.headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode("utf-8", errors="replace")
                    msg = f"HTTP {resp.status_code}: {_error_message(body)}"
                    if resp.status_code in RETRY_STATUS:
                        raise _Retryable(msg, resp.status_code, _retry_after(resp))
                    raise ProviderError(msg, resp.status_code)

                for line in resp.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        err = chunk["error"]
                        raise ProviderError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
                    if chunk.get("usage"):
                        result.usage = {k: v for k, v in chunk["usage"].items() if isinstance(v, int)}
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        if reasoning:
                            got_delta = True
                            reasoning_parts.append(reasoning)
                            if on_reasoning:
                                on_reasoning(reasoning)
                        content = delta.get("content") or ""
                        if content:
                            if not result.ttft_ms:
                                result.ttft_ms = round((time.time() - start) * 1000)
                            got_delta = True
                            text_parts.append(content)
                            if on_text:
                                on_text(content)
                        for tc in delta.get("tool_calls") or []:
                            got_delta = True
                            slot = calls.setdefault(self._slot_index(calls, tc),
                                                    {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            name = fn.get("name")
                            if name and name != slot["name"]:  # some backends resend the full name
                                slot["name"] += name
                            if fn.get("arguments"):
                                args = fn["arguments"]
                                slot["arguments"] += args if isinstance(args, str) else json.dumps(args)
                        if choice.get("finish_reason"):
                            result.finish_reason = choice["finish_reason"]
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if got_delta:
                raise ProviderError(f"Connection lost mid-stream: {e}") from None
            raise _Retryable(f"{type(e).__name__}: {e}") from None

        result.text = "".join(text_parts)
        result.reasoning = "".join(reasoning_parts)
        for idx in sorted(calls):
            slot = calls[idx]
            if slot["name"]:
                result.tool_calls.append(ToolCall(slot["id"] or f"call_{idx}_{int(start)}",
                                                  slot["name"], slot["arguments"] or "{}"))
        result.duration = time.time() - start
        return result

    @staticmethod
    def _slot_index(calls: Dict[int, Dict[str, str]], tc: Dict[str, Any]) -> int:
        if isinstance(tc.get("index"), int):
            return tc["index"]
        # No index: a new id starts a new call, otherwise the chunk continues the last one.
        cid = tc.get("id")
        for idx, slot in calls.items():
            if cid and slot["id"] == cid:
                return idx
        if cid or not calls:
            return max(calls, default=-1) + 1
        return max(calls)

    def complete(self, model: str, messages: List[Dict[str, Any]], max_tokens: int = 2048,
                 temperature: float = 0.2) -> str:
        """Non-streaming call used for compaction and summaries."""
        payload = {"model": model, "messages": normalize_messages(messages),
                   "max_tokens": max_tokens, "temperature": temperature, "stream": False}
        try:
            resp = self.client.post(f"{self.base_url}/chat/completions", headers=self.headers, json=payload)
        except httpx.HTTPError as e:
            raise ProviderError(f"{type(e).__name__}: {e}") from None
        if resp.status_code != 200:
            raise ProviderError(f"HTTP {resp.status_code}: {_error_message(resp.text)}")
        choices = resp.json().get("choices") or []
        if not choices:
            raise ProviderError("Empty response")
        return (choices[0].get("message") or {}).get("content") or ""

    def ping(self, model: str) -> Dict[str, Any]:
        start = time.time()
        try:
            text = self.complete(model, [{"role": "user", "content": "Reply OK"}], max_tokens=10)
            return {"ok": True, "latency_ms": round((time.time() - start) * 1000), "msg": text.strip()[:60]}
        except ProviderError as e:
            return {"ok": False, "latency_ms": round((time.time() - start) * 1000), "msg": str(e)[:120]}


class _Retryable(Exception):
    def __init__(self, message: str, status: Optional[int] = None, wait: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.wait = wait


def parse_arguments(raw: str) -> Dict[str, Any]:
    """Parse tool-call JSON, tolerating code fences and trailing junk from weaker models."""
    raw = (raw or "").strip() or "{}"
    candidates = [raw]
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    if fenced != raw:
        candidates.append(fenced)
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start:end + 1])
    for cand in candidates:
        try:
            val = json.loads(cand)
        except ValueError:
            continue
        if isinstance(val, dict):
            return val
    raise ValueError(f"Tool arguments are not a valid JSON object: {raw[:200]}")
