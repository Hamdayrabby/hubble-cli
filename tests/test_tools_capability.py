import json

import httpx

import hubble.providers as pv
from hubble.agent import Agent, Events
from hubble.permissions import Permissions
from hubble.provider import ProviderError, TurnResult
from hubble.scanner import _probe, tools_unsupported
from hubble.tools import ToolContext

VLLM_400 = ('{"error": {"message": "\\"auto\\" tool choice requires --enable-auto-tool-choice and '
            '--tool-call-parser to be set", "type": "BadRequestError"}}')


def test_detects_tool_unsupported_errors():
    assert tools_unsupported(VLLM_400)
    assert tools_unsupported("HTTP 400: model does not support tools")
    assert not tools_unsupported("HTTP 400: max_tokens too large")


def test_probe_marks_chat_only_deployment():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append("tools" in body)
        if "tools" in body:
            return httpx.Response(400, text=VLLM_400)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        r = _probe(c, "https://x/v1", {}, "nvidia/nemotron-parse-2.0", 5)
    assert r["available"] is True and r["tools"] is False and seen == [True, False]


def test_probe_tool_capable_model():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        assert _probe(c, "https://x/v1", {}, "m", 5)["tools"] is True


def test_fallback_skips_tool_less_and_non_chat_models(monkeypatch):
    pv.NO_TOOLS.clear()
    listing = [
        {"model": "nvidia/nemotron-parse-2.0", "available": True, "latency_ms": 100, "tools": True},  # name says parse
        {"model": "fast-chat-only", "available": True, "latency_ms": 150, "tools": False},
        {"model": "llama-embed-nemotron", "available": True, "latency_ms": 120},
        {"model": "good-agent", "available": True, "latency_ms": 900, "tools": True},
    ]
    monkeypatch.setattr(pv, "provider_models", lambda name: listing)
    s = {"fallback_model": "nothing-here"}
    assert pv.resolve_fallback(s, {"nvidia": 1}, "nvidia", "nemotron-ultra") == ("nvidia", "good-agent")
    pv.NO_TOOLS.add(("nvidia", "good-agent"))
    assert pv.resolve_fallback(s, {"nvidia": 1}, "nvidia", "nemotron-ultra") is None
    pv.NO_TOOLS.clear()


class Prov:
    def __init__(self, name, behaviour, log):
        self.hubble_name, self.behaviour, self.log = name, behaviour, log

    def stream(self, model, messages, **kw):
        self.log.append(model)
        b = self.behaviour.get(model)
        if isinstance(b, Exception):
            raise b
        return b or TurnResult(text=f"answer from {model}")


def test_tool_less_fallback_leads_to_the_next_candidate(tmp_path):
    pv.NO_TOOLS.clear()
    log = []
    p = Prov("nvidia", {"ultra": ProviderError("HTTP 503: Service temporarily overloaded", 503),
                        "parse-model": ProviderError(f"HTTP 400: {VLLM_400}", 400)}, log)
    chain = iter([(p, "parse-model"), (p, "good-agent")])
    agent = Agent(p, {"model": "ultra", "provider": "nvidia", "max_turns": 3, "max_tokens": 10,
                      "context_window": 1000, "auto_compact_ratio": 0, "persona": "code", "web_tools": False},
                  ToolContext(root=tmp_path), Permissions(), Events())
    agent.fallback_resolver = lambda prov, model: next(chain)
    assert agent.run("hi") == "answer from good-agent"
    assert log == ["ultra", "parse-model", "good-agent"]
    assert ("nvidia", "parse-model") in pv.NO_TOOLS
    pv.NO_TOOLS.clear()


def test_primary_without_tools_falls_back_instead_of_failing(tmp_path):
    pv.NO_TOOLS.clear()
    log = []
    p = Prov("nvidia", {"chat-only": ProviderError(f"HTTP 400: {VLLM_400}", 400)}, log)
    agent = Agent(p, {"model": "chat-only", "provider": "nvidia", "max_turns": 3, "max_tokens": 10,
                      "context_window": 1000, "auto_compact_ratio": 0, "persona": "code", "web_tools": False},
                  ToolContext(root=tmp_path), Permissions(), Events())
    agent.fallback_resolver = lambda prov, model: (p, "good-agent")
    assert agent.run("hi") == "answer from good-agent"
    pv.NO_TOOLS.clear()


def test_run_footer_shows_live_usage(tmp_path):
    from hubble.agent import RunStats
    from hubble.repl import Repl
    from hubble.session import SessionStore
    from hubble.ui import ReplEvents
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "nemotron-ultra", "provider": "nvidia", "max_turns": 3, "max_tokens": 10,
                         "context_window": 1000, "auto_compact_ratio": 0, "persona": "code"},
                  ctx, Permissions(), ReplEvents(ctx))
    repl = Repl(agent, SessionStore(tmp_path), {"nvidia": object(), "aihub": object()})
    agent.last_stats = RunStats(prompt_tokens=534_300, completion_tokens=1_200, tool_calls=34)
    agent.total_prompt_tokens, agent.total_completion_tokens = 1_200_000, 5_000
    line = repl._run_footer().plain
    assert "nvidia nemotron-ultra" in line and "↑ 534.3k" in line and "34 tool calls" in line
    assert "session 1.2M" in line
