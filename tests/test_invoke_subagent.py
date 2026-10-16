import json
import threading
from pathlib import Path

from hubble.agent import Agent, DefineSubagentTool, Events, InvokeSubagentTool, TaskTool
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.subagents import BUILTIN_AGENTS, discover_agents
from hubble.tools import ToolContext

BASE = {
    "max_turns": 6,
    "max_tokens": 100,
    "context_window": 100000,
    "auto_compact_ratio": 0,
    "persona": "code",
    "web_tools": False,
}


class MockProvider:
    def __init__(self, name, parent=None):
        self.hubble_name = name
        self.parent = list(parent or [])
        self.calls = []
        self.lock = threading.Lock()

    def stream(self, model, messages, **kw):
        sub = len(messages) > 0 and "sub-agent" in str(messages[0].get("content", ""))
        with self.lock:
            self.calls.append((model, messages))
        if sub:
            return TurnResult(text="subagent report content")
        if self.parent:
            return self.parent.pop(0)
        return TurnResult(text="Done")


def test_builtin_agents_discovered(tmp_path):
    defs = BUILTIN_AGENTS
    assert "research" in defs
    assert "self" in defs
    assert "code-reviewer" in defs
    assert "test-writer" in defs

    research = defs["research"]
    assert research.capability == "read_only"
    assert "read_file" in research.tools

    self_agent = defs["self"]
    assert self_agent.capability == "edit"

    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {**BASE, "model": "test-model"}, ctx, Permissions(), Events())
    invoker = next(t for t in agent.tools if t.name == "invoke_subagent")
    enum_vals = invoker.parameters["properties"]["subagent_type"]["enum"]
    for expected in ("research", "self", "code-reviewer", "test-writer"):
        assert expected in enum_vals


def test_invoke_subagent_tool_execution(tmp_path):
    prov = MockProvider("testprov", parent=[
        TurnResult(tool_calls=[
            ToolCall("c1", "invoke_subagent", json.dumps({
                "description": "explore codebase",
                "prompt": "search for entry point",
                "subagent_type": "research",
            })),
        ]),
        TurnResult(text="Done exploring"),
    ])
    ctx = ToolContext(root=tmp_path)
    agent = Agent(prov, {**BASE, "model": "test-model", "provider": "testprov"}, ctx, Permissions(), Events())
    agent.client_for = {"testprov": prov}.get

    result = agent.run("start task")
    assert result == "Done exploring"
    # Verify subagent call received the research specialist prompt
    sub_calls = [c for c in prov.calls if len(c[1]) > 0 and "research specialist" in c[1][0].get("content", "")]
    assert len(sub_calls) == 1


def test_define_subagent_tool_creates_and_registers(tmp_path):
    prov = MockProvider("testprov")
    ctx = ToolContext(root=tmp_path)
    agent = Agent(prov, {**BASE, "model": "test-model", "provider": "testprov"}, ctx, Permissions(), Events())

    tool = DefineSubagentTool(agent)
    res = tool.run({
        "name": "api-docs-generator",
        "description": "Generates OpenAPI specification from python routes",
        "system_prompt": "You analyze fastapi/flask routes and output openapi.json.",
        "capability": "read_only",
        "tools": ["read_file", "grep", "glob"],
        "scope": "project",
    }, ctx)

    assert "Successfully defined subagent 'api-docs-generator'" in res
    assert (tmp_path / ".hubble" / "agents" / "api-docs-generator.md").is_file()

    # Verify agent is immediately in agent.agent_defs
    def_names = {d.name for d in agent.agent_defs}
    assert "api-docs-generator" in def_names

    # Test invoking the newly defined subagent
    invoker = InvokeSubagentTool(agent)
    assert "api-docs-generator" in invoker.parameters["properties"]["subagent_type"]["enum"]
