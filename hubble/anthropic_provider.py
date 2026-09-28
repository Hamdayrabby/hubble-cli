"""Native Claude provider (Anthropic Messages API) with the same interface as OpenAICompatProvider.

Hubble keeps its conversation history in OpenAI chat format; this module translates it to
Messages API requests (system prompt, content blocks, tool_use / tool_result, images) and the
streamed reply back into a TurnResult. The assistant's own content blocks (thinking blocks with
their signatures included) are kept on the message under `_anthropic_content` and sent back
unchanged on the next request, as the API expects when continuing a tool-use turn.

Uses the official `anthropic` SDK: it handles SSE parsing, retries (429 / 5xx / 529 overloaded)
and partial tool-input JSON.
"""

import json
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from hubble.provider import ProviderError, ToolCall, TurnResult, parse_arguments, pump

DEFAULT_BASE_URL = "https://api.anthropic.com"
# Models where the API's server-side refusal fallback is recommended (and supported).
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1", "claude-mythos-5-1")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls",
                "max_tokens": "length", "refusal": "refusal", "pause_turn": "pause_turn"}
ID_RX = re.compile(r"[^a-zA-Z0-9_-]")


def anthropic_base_url(url: str) -> str:
    """The SDK adds /v1/... itself, so drop a trailing /v1 that normalize_base_url may have added."""
    url = (url or DEFAULT_BASE_URL).strip().rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def _tool_id(raw: str) -> str:
    """Tool-use ids must match ^[a-zA-Z0-9_-]+$; ids from other providers may not."""
    return ID_RX.sub("_", raw or "") or "toolu_x"


def _image_block(url: str) -> Optional[Dict[str, Any]]:
    m = re.match(r"^data:(image/[\w.+-]+);base64,(.+)$", url or "", re.DOTALL)
    if m:
        return {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _user_blocks(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text"):
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            img = _image_block((part.get("image_url") or {}).get("url", ""))
            if img:
                blocks.append(img)
    return blocks


def to_anthropic(messages: List[Dict[str, Any]]):
    """OpenAI-format history -> (system text, Messages API messages)."""
    system_parts: List[str] = []
    out: List[Dict[str, Any]] = []

    def add(role: str, blocks: List[Dict[str, Any]]):
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)  # tool results of one turn share a single user message
        else:
            out.append({"role": role, "content": list(blocks)})

    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "system":
            if i == 0:
                system_parts.append(m.get("content") or "")
            else:
                add("user", [{"type": "text", "text": f"<system-note>\n{m.get('content') or ''}\n</system-note>"}])
        elif role == "user":
            add("user", _user_blocks(m.get("content")))
        elif role == "assistant":
            raw = m.get("_anthropic_content")
            if raw:
                add("assistant", [dict(b) for b in raw])  # exact blocks from Claude, thinking included
                continue
            blocks: List[Dict[str, Any]] = []
            text = m.get("content") or ""
            if isinstance(text, str) and text.strip():
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = parse_arguments(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": _tool_id(tc.get("id", "")), "name": fn.get("name", ""),
                               "input": args})
            add("assistant", blocks or [{"type": "text", "text": "(no content)"}])
        elif role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": _tool_id(m.get("tool_call_id", "")),
                          "content": m.get("content") or "(no output)",
                          **({"is_error": True} if str(m.get("content", "")).startswith(("Error:", "Permission denied"))
                             else {})}])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation continues)"}]})
    return "\n\n".join(p for p in system_parts if p), out


def to_anthropic_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out = []
    for t in tools or []:
        fn = t.get("function") or t
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        # Stream large tool inputs (file contents) as they are generated; Hubble validates every
        # parsed input against the tool's schema before running it.
        out.append({"name": fn["name"], "description": fn.get("description", "")[:4096],
                    "input_schema": schema, "eager_input_streaming": True})
    return out


class AnthropicProvider:
    """Talks to Claude through the official SDK. Same methods as OpenAICompatProvider."""

    kind = "anthropic"

    def __init__(self, base_url: str, api_key: str, client=None, max_retries: int = 3):
        import anthropic
        from hubble import __version__
        self._anthropic = anthropic
        self.base_url = anthropic_base_url(base_url)
        self.api_key = api_key
        self.client = client or anthropic.Anthropic(
            api_key=api_key, base_url=self.base_url, max_retries=max_retries, timeout=600.0,
            default_headers={"User-Agent": f"hubble-cli/{__version__} (+https://github.com/Hamdayrabby/hubble-cli)"})
        # Server-side fallbacks are a first-party API feature; proxies and gateways may reject them.
        self.first_party = urlparse(self.base_url).hostname == "api.anthropic.com"

    # ----- errors ---------------------------------------------------------

    def _error(self, e: Exception) -> ProviderError:
        a = self._anthropic
        if isinstance(e, a.APIStatusError):
            msg = getattr(e, "message", None) or str(e)
            return ProviderError(f"HTTP {e.status_code}: {str(msg)[:200]}", e.status_code)
        if isinstance(e, a.APIConnectionError):
            return ProviderError(f"{type(e).__name__}: {e}")  # status None -> transient
        return ProviderError(f"{type(e).__name__}: {e}", 400)

    # ----- requests -------------------------------------------------------

    def _params(self, model: str, messages, tools, max_tokens: int) -> Dict[str, Any]:
        system, msgs = to_anthropic(messages)
        params: Dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": msgs}
        if system:
            # Cache the stable prefix (tools + system prompt) across the turns of a session.
            params["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            params["tools"] = to_anthropic_tools(tools)
        # No temperature and no thinking config: current Claude models reject sampling
        # parameters, and pick their own (adaptive) thinking by default.
        return params

    def _use_fallbacks(self, model: str) -> bool:
        return self.first_party and any(model == m or model.startswith(m + "-") for m in FALLBACK_MODELS)

    def stream(self, model: str, messages: List[Dict[str, Any]], tools: Optional[List[Dict]] = None,
               temperature: float = 0.3, max_tokens: int = 8192,
               on_text: Optional[Callable[[str], None]] = None,
               on_reasoning: Optional[Callable[[str], None]] = None) -> TurnResult:
        # Thinking counts against max_tokens and the reply is streamed (no timeout risk), so give
        # the model room: Hubble's generic 8192 default would cut deep answers short.
        params = self._params(model, messages, tools, max(max_tokens, 32000))
        for attempt in range(3):
            try:
                return self._stream_once(model, params, on_text, on_reasoning)
            except ValueError as e:
                # Tool-input JSON the SDK could not parse at all (possible with eager input
                # streaming). No tool_use id exists to answer, so re-issue the turn.
                if attempt == 2:
                    raise ProviderError(f"Claude sent unparseable tool input: {e}", 400) from None
            except self._anthropic.APIError as e:
                raise self._error(e) from None
        raise ProviderError("unreachable")

    def _stream_once(self, model, params, on_text, on_reasoning) -> TurnResult:
        start = time.time()
        result = TurnResult()
        state: Dict[str, Any] = {}

        def produce(put):
            if self._use_fallbacks(model):
                ctx = self.client.beta.messages.stream(**params, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                ctx = self.client.messages.stream(**params)
            with ctx as stream:
                state["stream"] = stream
                for event in stream:
                    put(("event", event))
                put(("final", stream.get_final_message()))

        def abort():
            if state.get("stream") is not None:
                state["stream"].close()

        final = None
        # The SDK read runs in a worker thread so Ctrl+C lands at once, even mid-thinking.
        for kind, value in pump(produce, abort):
            if kind == "final":
                final = value
                continue
            event = value
            if event.type == "text" and event.text:
                if not result.ttft_ms:
                    result.ttft_ms = round((time.time() - start) * 1000)
                if on_text:
                    on_text(event.text)
            elif event.type == "thinking" and getattr(event, "thinking", ""):
                if on_reasoning:
                    on_reasoning(event.thinking)
        if final is None:
            raise ProviderError("Claude's stream ended without a final message")
        data = final.to_dict()
        blocks = data.get("content") or []
        result.text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        result.reasoning = "".join(b.get("thinking", "") for b in blocks if b.get("type") == "thinking")
        stop = data.get("stop_reason")
        result.finish_reason = STOP_REASONS.get(stop, stop)
        if stop != "max_tokens":
            # On max_tokens a tool input may be cut off mid-JSON: never run it.
            result.tool_calls = [ToolCall(b["id"], b["name"], json.dumps(b.get("input") or {}))
                                 for b in blocks if b.get("type") == "tool_use"]
        u = data.get("usage") or {}
        prompt = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                   "cache_creation_input_tokens"))
        result.usage = {"prompt_tokens": prompt, "completion_tokens": int(u.get("output_tokens") or 0),
                        "total_tokens": prompt + int(u.get("output_tokens") or 0)}
        if stop != "max_tokens":
            # Echo Claude's own blocks back next turn (keeps thinking signatures intact).
            result.raw_content = blocks
        result.duration = time.time() - start
        return result

    def complete(self, model: str, messages: List[Dict[str, Any]], max_tokens: int = 2048,
                 temperature: float = 0.2) -> str:
        params = self._params(model, messages, None, max_tokens)
        try:
            with self.client.messages.stream(**params) as stream:
                final = stream.get_final_message()
        except self._anthropic.APIError as e:
            raise self._error(e) from None
        return "".join(b.text for b in final.content if b.type == "text")

    def ping(self, model: str) -> Dict[str, Any]:
        start = time.time()
        try:
            text = self.complete(model, [{"role": "user", "content": "Reply OK"}], max_tokens=64)
            return {"ok": True, "latency_ms": round((time.time() - start) * 1000), "msg": text.strip()[:60]}
        except ProviderError as e:
            return {"ok": False, "latency_ms": round((time.time() - start) * 1000), "msg": str(e)[:120]}

    def list_models(self) -> List[Dict[str, Any]]:
        """[{id, context_length, max_output}] from GET /v1/models."""
        try:
            return [{"id": m.id, "context_length": getattr(m, "max_input_tokens", None),
                     "max_output": getattr(m, "max_tokens", None)} for m in self.client.models.list()]
        except self._anthropic.APIError as e:
            raise self._error(e) from None

