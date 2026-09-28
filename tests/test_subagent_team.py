import json
import threading

from hubble.agent import Agent, Events, TaskTool, team_block
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.tools import ToolContext

BASE = {"max_turns": 6, "max_tokens": 100, "context_window": 100000, "auto_compact_ratio": 0,
        "persona": "code", "web_tools": False}


class Prov:
    """Parent turns come from `parent`; any sub-agent call answers with which model it ran on."""

    def __init__(self, name, log, parent=None):
        self.hubble_name, self.log, self.parent = name, log, list(parent or [])
        self.lock = threading.Lock()

    def stream(self, model, messages, **kw):
        sub = "sub-agent" in messages[0]["content"]
        with self.lock:
            self.log.append((self.hubble_name, model, "sub" if sub else "main"))
        if sub:
            return TurnResult(text=f"report from {self.hubble_name}:{model}")
        return self.parent.pop(0)


class Rec(Events):
    def __init__(self):
        self.labels, self.notices = [], []

    def subagent_start(self, key, label):
        self.labels.append(label)

    def notice(self, m, level="info"):
        self.notices.append(m)


def task(cid, desc, model=None, **extra):
    args = {"description": desc, "prompt": f"do {desc}", **extra}
    if model:
        args["model"] = model
    return ToolCall(cid, "task", json.dumps(args))


def test_parallel_subagents_each_on_their_own_model_and_provider(tmp_path):
    log = []
    aihub = Prov("aihub", log, parent=[
        TurnResult(tool_calls=[task("t1", "search code", "aihub:small-fast"),
                               task("t2", "check docs", "groq:llama-8b"),
                               task("t3", "review design", "anthropic:claude-opus-5")]),
        TurnResult(text="combined answer"),
    ])
    provs = {"aihub": aihub, "groq": Prov("groq", log), "anthropic": Prov("anthropic", log)}
    events = Rec()
    agent = Agent(aihub, {**BASE, "model": "main-model", "provider": "aihub"}, ToolContext(root=tmp_path),
                  Permissions(), events)
    agent.client_for = provs.get
    assert agent.run("investigate and review") == "combined answer"
    subs = {(p, m) for p, m, kind in log if kind == "sub"}
    assert subs == {("aihub", "small-fast"), ("groq", "llama-8b"), ("anthropic", "claude-opus-5")}
    reports = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert "report from groq:llama-8b" in reports[1] and "anthropic:claude-opus-5" in reports[2]
    assert any("· llama-8b" in label for label in events.labels)  # the UI shows each sub-agent's model


def test_same_provider_model_name_still_works(tmp_path):
    log = []
    aihub = Prov("aihub", log, parent=[TurnResult(tool_calls=[task("t1", "look", "other-model")]),
                                       TurnResult(text="ok")])
    agent = Agent(aihub, {**BASE, "model": "main", "provider": "aihub"}, ToolContext(root=tmp_path),
                  Permissions(), Events())
    agent.run("go")
    assert ("aihub", "other-model", "sub") in log


def test_unknown_provider_falls_back_to_parent_model_with_notice(tmp_path):
    log = []
    aihub = Prov("aihub", log, parent=[TurnResult(tool_calls=[task("t1", "look", "nope:x")]),
                                       TurnResult(text="ok")])
    events = Rec()
    agent = Agent(aihub, {**BASE, "model": "main", "provider": "aihub"}, ToolContext(root=tmp_path),
                  Permissions(), events)
    agent.client_for = {"aihub": aihub}.get
    agent.run("go")
    assert ("aihub", "main", "sub") in log
    assert any("not configured" in n for n in events.notices)


def test_team_listed_in_task_tool_description_with_stats():
    import hubble.stats as stats
    log = stats.StatsLog()
    for _ in range(3):
        log.call("aihub", "small-fast", True, completion_tokens=100, duration=1.0)
    settings = {"provider": "aihub", "subagent_models": [
        {"model": "aihub:small-fast", "use": "fast, for searching"},
        {"model": "anthropic:claude-opus-5", "use": "careful, for review"}]}
    import hubble.agent as agent_mod
    agent_mod._TEAM_CACHE.update(at=0.0, key=None)
    text = team_block(settings)
    assert "aihub:small-fast: fast, for searching [100% of 3 calls ok, 100 tok/s]" in text
    assert "anthropic:claude-opus-5: careful, for review" in text

    class P:
        pass
    parent = P()
    parent.settings, parent.agent_defs = settings, []
    desc = TaskTool(parent).description
    assert "Model team for sub-agents" in desc and "claude-opus-5" in desc
    assert team_block({}) == ""


def test_agent_definition_can_pin_a_model_on_another_provider(tmp_path, monkeypatch):
    import hubble.plugins as plugins
    import hubble.subagents as subagents
    monkeypatch.setattr(subagents, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(plugins, "HOME_DIR", tmp_path / "home")
    d = tmp_path / ".hubble" / "agents"
    d.mkdir(parents=True)
    (d / "reviewer.md").write_text("---\nname: reviewer\ndescription: Reviews\nmodel: anthropic:claude-opus-5\n---\n"
                                   "You review code.\n", encoding="utf-8")
    log = []
    aihub = Prov("aihub", log, parent=[TurnResult(tool_calls=[
        ToolCall("t1", "task", json.dumps({"description": "review", "prompt": "review it", "agent": "reviewer"}))]),
        TurnResult(text="ok")])
    provs = {"aihub": aihub, "anthropic": Prov("anthropic", log)}
    agent = Agent(aihub, {**BASE, "model": "main", "provider": "aihub"}, ToolContext(root=tmp_path),
                  Permissions(), Events())
    agent.client_for = provs.get
    agent.run("review my change")
    assert ("anthropic", "claude-opus-5", "sub") in log


def test_team_command(tmp_path, monkeypatch):
    import hubble.repl as repl_mod
    from hubble.repl import Repl
    from hubble.session import SessionStore
    from hubble.ui import ReplEvents
    saved = {}
    monkeypatch.setattr(repl_mod, "save_user_setting", lambda k, v: saved.__setitem__(k, v))
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {**BASE, "model": "m", "provider": "aihub"}, ctx, Permissions(), ReplEvents(ctx))
    repl = Repl(agent, SessionStore(tmp_path), {"aihub": object(), "groq": object()})
    repl.handle("/team add groq:llama-8b fast, for searching")
    repl.handle("/team add ministral-8b-latest")
    assert saved["subagent_models"] == [{"model": "groq:llama-8b", "use": "fast, for searching"},
                                        {"model": "aihub:ministral-8b-latest", "use": ""}]
    repl.handle("/team add nope:x")                      # unknown provider: refused
    assert len(saved["subagent_models"]) == 2
    repl.handle("/team remove 1")
    assert saved["subagent_models"] == [{"model": "aihub:ministral-8b-latest", "use": ""}]
    repl.handle("/team clear")
    assert saved["subagent_models"] == []
