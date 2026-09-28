import json

import hubble.stats as stats
from hubble.agent import Agent, Events
from hubble.permissions import Permissions
from hubble.provider import ProviderError, ToolCall, TurnResult
from hubble.router import Struggle, classify, parse_spec, routing_config
from hubble.tools import ToolContext

BASE = {"max_turns": 6, "max_tokens": 100, "context_window": 100000, "auto_compact_ratio": 0,
        "persona": "code", "web_tools": False}


class Named:
    """Scripted provider: pops replies, records which model each call used."""

    def __init__(self, name, replies, log):
        self.hubble_name, self.replies, self.log = name, list(replies), log

    def stream(self, model, messages, **kw):
        self.log.append((self.hubble_name, model, messages[0]["content"][:30]))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make(tmp_path, providers, settings=None, main="aihub", model="big"):
    agent = Agent(providers[main], {**BASE, "model": model, "provider": main, **(settings or {})},
                  ToolContext(root=tmp_path), Permissions("accept-edits"), Events())
    agent.client_for = providers.get
    return agent


# ----- classifier -----------------------------------------------------------

def test_classify_questions_fast_changes_strong():
    assert classify("what does the scanner do?").tier == "fast"
    assert classify("explain how routing works").tier == "fast"
    assert classify("hi").tier == "fast"
    assert classify("fix the failing test in tests/test_core.py").tier == "strong"
    assert classify("refactor the provider module and add retries").tier == "strong"
    assert classify("why is this failing?\nTraceback (most recent call last):\n  File x").tier == "strong"
    r = classify("implement a cache")
    assert r.tier == "strong" and any("implement" in x for x in r.reasons)


def test_parse_spec_handles_slashes_and_providers():
    assert parse_spec("nvidia/nemotron-3-super-120b-a12b", "aihub") == ("aihub", "nvidia/nemotron-3-super-120b-a12b")
    assert parse_spec("nvidia:meta/llama-3.1-8b", "aihub") == ("nvidia", "meta/llama-3.1-8b")
    assert parse_spec("codestral-latest", "hcnsec") == ("hcnsec", "codestral-latest")


def test_routing_config_needs_both_tiers_and_respects_off():
    assert routing_config({}) is None
    assert routing_config({"routing": {"fast": "a"}}) is None
    assert routing_config({"routing": {"fast": "a", "strong": "b", "enabled": False}}) is None
    assert routing_config({"routing": {"fast": "a", "strong": "b"}}) == {"fast": "a", "strong": "b"}


def test_struggle_signals():
    s = Struggle()
    s.note_tool("Error: file not found", True)
    assert s.reason() is None
    s.note_turn("", 0)
    assert "empty" in s.reason()
    s2 = Struggle()
    for _ in range(2):
        s2.note_tool("Error: missing required argument 'path'", True)
    assert "malformed" in s2.reason()
    s3 = Struggle()
    s3.note_tool("The user denied this tool call.", True)
    assert s3.reason() is None  # a user saying no is not the model struggling


# ----- routing in the agent loop --------------------------------------------

def test_prompt_routed_to_fast_then_model_restored(tmp_path):
    log = []
    provs = {"aihub": Named("aihub", [TurnResult(text="It scans models.")], log)}
    agent = make(tmp_path, provs, {"routing": {"fast": "aihub:small", "strong": "aihub:big"}})
    assert agent.run("what does the scanner do?") == "It scans models."
    assert log[0][1] == "small"
    assert agent.model == "big"                      # restored after the task
    assert agent.last_route[0] == "fast"


def test_route_across_providers(tmp_path):
    log = []
    provs = {"aihub": Named("aihub", [], log),
             "groq": Named("groq", [TurnResult(text="done")], log)}
    agent = make(tmp_path, provs, {"routing": {"fast": "groq:llama-fast", "strong": "aihub:big"}})
    agent.run("list the files")
    assert log == [("groq", "llama-fast", log[0][2])]
    assert agent.provider is provs["aihub"] and agent.provider_name == "aihub"


def test_fast_model_empty_reply_escalates_to_strong(tmp_path):
    log = []
    provs = {"aihub": Named("aihub", [TurnResult(text=""), TurnResult(text="real answer")], log)}
    agent = make(tmp_path, provs, {"routing": {"fast": "aihub:small", "strong": "aihub:big"}})
    assert agent.run("where is the config loaded?") == "real answer"
    assert [m for _, m, _ in log] == ["small", "big"]
    recs = stats.load()
    task = [r for r in recs if r["type"] == "task"][-1]
    assert task["escalated"] and task["tier"] == "fast" and task["ok"] and task["model"] == "big"


def test_repeated_bad_tool_calls_escalate(tmp_path):
    log = []
    bad = TurnResult(tool_calls=[ToolCall("c1", "read_file", json.dumps({}))])  # missing required path
    provs = {"aihub": Named("aihub", [bad, TurnResult(tool_calls=[ToolCall("c2", "read_file", "{}")]),
                                      TurnResult(text="ok")], log)}
    agent = make(tmp_path, provs, {"routing": {"fast": "aihub:small", "strong": "aihub:big"}})
    agent.run("show me the readme")
    assert [m for _, m, _ in log] == ["small", "small", "big"]


def test_research_subagent_uses_fast_tier(tmp_path):
    log = []
    provs = {"aihub": Named("aihub", [
        TurnResult(tool_calls=[ToolCall("t1", "task", json.dumps({"description": "look", "prompt": "find x"}))]),
        TurnResult(text="report"),
        TurnResult(text="final"),
    ], log)}
    agent = make(tmp_path, provs, {"routing": {"fast": "aihub:small", "strong": "aihub:big"}})
    agent.run("refactor the scanner to use the cache")   # strong prompt
    assert [m for _, m, _ in log] == ["big", "small", "big"]  # parent strong, research sub-agent fast


def test_no_routing_leaves_model_alone(tmp_path):
    log = []
    provs = {"aihub": Named("aihub", [TurnResult(text="x")], log)}
    agent = make(tmp_path, provs)
    agent.run("what is this")
    assert log[0][1] == "big" and agent.last_route is None


# ----- stats ------------------------------------------------------------------

def test_calls_tasks_and_fallback_are_recorded(tmp_path):
    log = []
    provs = {"aihub": Named("aihub", [ProviderError("HTTP 429: busy", 429)], log),
             "groq": Named("groq", [TurnResult(text="answer", usage={"prompt_tokens": 100, "completion_tokens": 20},
                                               duration=2.0)], log)}
    agent = make(tmp_path, provs)
    agent.fallback_resolver = lambda prov, model: (provs["groq"], "llama")
    assert agent.run("hello there") == "answer"
    recs = stats.load()
    calls = [r for r in recs if r["type"] == "call"]
    assert calls[0]["ok"] is False and calls[0]["status"] == 429 and calls[0]["model"] == "big"
    assert calls[1]["ok"] and calls[1]["provider"] == "groq" and calls[1]["model"] == "llama" and calls[1]["fallback"]
    assert calls[1]["in"] == 100 and calls[1]["out"] == 20
    rows = {r["model"]: r for r in stats.summarize(recs)}
    assert rows["big"]["errors"] == {"429": 1} and rows["llama"]["tok_per_s"] == 10.0


def test_summary_success_rates_cost_and_fast_pick(tmp_path):
    log = stats.StatsLog()
    for _ in range(6):
        log.call("aihub", "quick", True, prompt_tokens=1000, completion_tokens=500, duration=1.0)
    log.call("aihub", "quick", False, status=503)
    for _ in range(6):
        log.call("anthropic", "claude-opus-5", True, prompt_tokens=1_000_000, completion_tokens=100_000, duration=1000.0)
    log.task("aihub", "quick", True)
    log.task("aihub", "quick", False, reason="no answer")
    log.task("aihub", "quick", True, subagent=True)  # sub-agent tasks don't count as your tasks
    rows = {r["model"]: r for r in stats.summarize(stats.load())}
    q = rows["quick"]
    assert q["calls"] == 7 and round(q["call_success"], 2) == 0.86 and q["tasks"] == 2 and q["task_success"] == 0.5
    assert q["cost"] is None and q["tok_per_s"] == 500.0
    assert round(rows["claude-opus-5"]["cost"], 2) == round(6 * (5.0 + 2.5), 2)  # list price
    best = stats.best_fast_model(list(rows.values()), min_success=0.8)
    assert best["model"] == "quick"
    assert stats.best_fast_model(list(rows.values()), exclude=["quick"], min_success=0.8)["model"] == "claude-opus-5"
    priced = {r["model"]: r for r in stats.summarize(stats.load(), {"quick": [1.0, 2.0]})}
    assert priced["quick"]["cost"] == (6000 * 1.0 + 3000 * 2.0) / 1e6


def test_stats_disabled(tmp_path):
    log = []
    provs = {"aihub": Named("aihub", [TurnResult(text="x")], log)}
    make(tmp_path, provs, {"stats": False}).run("hi")
    assert stats.load() == []


def test_render_table_smoke():
    from rich.console import Console
    import io
    log = stats.StatsLog()
    log.call("aihub", "m", True, prompt_tokens=10, completion_tokens=5, duration=0.5, ttft_ms=300)
    log.task("aihub", "m", True)
    con = Console(file=io.StringIO(), width=140)
    con.print(stats.render(stats.summarize(stats.load()), 30))
    out = con.file.getvalue()
    assert "tasks ok" in out and "1/1 100%" in out and "last 30 days" in out
    con2 = Console(file=io.StringIO(), width=100)
    con2.print(stats.render([], 7))
    assert "No usage recorded" in con2.file.getvalue()
