import json

from hubble.agent import Agent, Events
from hubble.hooks import HookResult
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.tools import ToolContext

SETTINGS = {"model": "m", "max_turns": 6, "max_tokens": 100, "context_window": 100000,
            "auto_compact_ratio": 0, "persona": "code"}


class FakeProvider:
    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, model, messages, **kw):
        self.last_messages = messages
        return self.turns.pop(0)

    def complete(self, model, messages, **kw):
        return "SUMMARY"


class RecordingHooks:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def run(self, event, payload, name=None):
        self.calls.append((event, payload))
        return self.responses.get(event, HookResult())


class YesEvents(Events):
    def ask(self, tool, args, preview):
        return "yes", ""


def make(tmp_path, turns, responses=None):
    agent = Agent(FakeProvider(turns), dict(SETTINGS), ToolContext(root=tmp_path), Permissions(), YesEvents())
    agent.hooks = RecordingHooks(responses)
    return agent


def events_of(agent):
    return [e for e, _ in agent.hooks.calls]


def test_session_start_context_goes_into_system_prompt(tmp_path):
    agent = make(tmp_path, [TurnResult(text="ok")],
                 {"SessionStart": HookResult(additional_context="branch: feature/x")})
    agent.start_session()
    agent.run("hi")
    assert "branch: feature/x" in agent.provider.last_messages[0]["content"]
    agent.shutdown()
    assert events_of(agent)[0] == "SessionStart" and events_of(agent)[-1] == "SessionEnd"


def test_session_end_not_run_without_start(tmp_path):
    agent = make(tmp_path, [])
    agent.shutdown()
    assert "SessionEnd" not in events_of(agent)


def test_notification_fires_before_permission_prompt(tmp_path):
    agent = make(tmp_path, [
        TurnResult(tool_calls=[ToolCall("c1", "write_file", json.dumps({"path": "a.txt", "content": "x"}))]),
        TurnResult(text="done"),
    ])
    agent.run("write a file")
    names = events_of(agent)
    assert "Notification" in names and names.index("Notification") < names.index("PostToolUse")


def test_pre_compact_can_block(tmp_path):
    agent = make(tmp_path, [], {"PreCompact": HookResult(decision="block", reason="keep history")})
    agent.messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                      {"role": "user", "content": "c"}]
    assert agent.compact() is False
    assert len(agent.messages) == 3


def test_subagent_stop_adds_context_to_report(tmp_path):
    agent = make(tmp_path, [
        TurnResult(tool_calls=[ToolCall("c1", "task", json.dumps({"description": "look", "prompt": "p"}))]),
        TurnResult(text="sub report"),
        TurnResult(text="final"),
    ], {"SubagentStop": HookResult(additional_context="verified by hook")})
    agent.run("research")
    tool_msg = next(m for m in agent.messages if m.get("role") == "tool")
    assert "sub report" in tool_msg["content"] and "verified by hook" in tool_msg["content"]
