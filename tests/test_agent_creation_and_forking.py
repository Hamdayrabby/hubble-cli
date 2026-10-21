import json
from pathlib import Path
from unittest.mock import patch

import hubble.plugins as plugins
import hubble.settings as settings_mod
import hubble.skills as skills_mod
import hubble.subagents as subagents
from hubble.agent import Agent, DefineSubagentTool, Events, InvokeSubagentTool, TaskTool
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.repl import Repl
from hubble.session import SessionStore
from hubble.subagents import AgentDef, BUILTIN_AGENTS, discover_agents, save_agent_definition
from hubble.tools import ToolContext
from hubble.ui import ReplEvents

SETTINGS = {
    "model": "m",
    "max_turns": 6,
    "max_tokens": 100,
    "context_window": 100000,
    "auto_compact_ratio": 0,
    "persona": "code",
    "web_tools": False,
}


class RecordingProvider:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def stream(self, model, messages, tools=None, on_text=None, **kw):
        self.calls.append({
            "model": model,
            "system": messages[0]["content"] if messages else "",
            "messages": [dict(m) for m in messages],
            "tools": [t["function"]["name"] for t in tools or []],
        })
        return self.turns.pop(0) if self.turns else TurnResult(text="Done")


def isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    for mod in (plugins, subagents, skills_mod, settings_mod):
        monkeypatch.setattr(mod, "HOME_DIR", home)
    return home


def test_agent_def_fields_and_defaults():
    # Test default values
    agent = AgentDef(
        name="tester",
        description="A test agent",
        prompt="You test code.",
        path=Path("tester.md"),
        scope="project",
    )
    assert agent.color == ""
    assert agent.fork_context is False

    # Test custom values
    agent_custom = AgentDef(
        name="custom",
        description="Custom agent",
        prompt="You do custom work.",
        path=Path("custom.md"),
        scope="user",
        color="magenta",
        fork_context=True,
    )
    assert agent_custom.color == "magenta"
    assert agent_custom.fork_context is True


def test_save_and_discover_agent_definition_with_color_and_fork_context(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)

    saved = save_agent_definition(
        root=tmp_path,
        name="forker",
        description="Forks context and uses magenta",
        prompt="You are a context-forking specialist.",
        capability="edit",
        color="magenta",
        fork_context=True,
        scope="project",
    )
    assert saved.color == "magenta"
    assert saved.fork_context is True
    assert saved.path.exists()

    content = saved.path.read_text(encoding="utf-8")
    assert "color: magenta" in content
    assert "fork_context: true" in content

    # Discovery
    defs = {a.name: a for a in discover_agents(tmp_path)}
    assert "forker" in defs
    discovered = defs["forker"]
    assert discovered.color == "magenta"
    assert discovered.fork_context is True
    assert discovered.capability == "edit"
    assert "context-forking specialist" in discovered.prompt


def test_discover_agents_fork_context_boolean_parsing(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    d = tmp_path / ".hubble" / "agents"
    d.mkdir(parents=True, exist_ok=True)

    # Test explicit fork_context strings in frontmatter
    (d / "a1.md").write_text(
        "---\nname: a1\ndescription: Agent 1\nfork_context: true\ncolor: cyan\n---\nPrompt 1",
        encoding="utf-8",
    )
    (d / "a2.md").write_text(
        "---\nname: a2\ndescription: Agent 2\nfork_context: false\ncolor: yellow\n---\nPrompt 2",
        encoding="utf-8",
    )
    (d / "a3.md").write_text(
        "---\nname: a3\ndescription: Agent 3\n---\nPrompt 3",
        encoding="utf-8",
    )

    defs = {a.name: a for a in discover_agents(tmp_path)}
    assert defs["a1"].fork_context is True
    assert defs["a1"].color == "cyan"

    assert defs["a2"].fork_context is False
    assert defs["a2"].color == "yellow"

    assert defs["a3"].fork_context is False
    assert defs["a3"].color == ""


def test_define_subagent_tool_with_color_and_fork_context(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    ctx = ToolContext(root=tmp_path)
    prov = RecordingProvider([])
    agent = Agent(prov, dict(SETTINGS), ctx, Permissions(), Events())

    tool = DefineSubagentTool(agent)
    schema = tool.parameters["properties"]
    assert "color" in schema
    assert "fork_context" in schema

    res = tool.run({
        "name": "data-analyst",
        "description": "Analyzes complex datasets",
        "system_prompt": "You are a data analysis agent.",
        "capability": "read_only",
        "color": "green",
        "fork_context": True,
        "scope": "project",
    }, ctx)

    assert "Successfully defined subagent 'data-analyst'" in res
    agent_file = tmp_path / ".hubble" / "agents" / "data-analyst.md"
    assert agent_file.is_file()

    content = agent_file.read_text(encoding="utf-8")
    assert "color: green" in content
    assert "fork_context: true" in content

    # Check updated agent_defs in parent
    analyst_def = next(d for d in agent.agent_defs if d.name == "data-analyst")
    assert analyst_def.color == "green"
    assert analyst_def.fork_context is True


def test_task_tool_and_invoke_subagent_tool_parameters():
    tool = TaskTool(parent=None)
    assert "fork_context" in tool.parameters["properties"]
    assert tool.parameters["properties"]["fork_context"]["type"] == "boolean"

    invoker = InvokeSubagentTool(parent=None)
    assert "fork_context" in invoker.parameters["properties"]
    assert invoker.parameters["properties"]["fork_context"]["type"] == "boolean"


def test_task_tool_forks_parent_context_when_flag_is_true(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    prov = RecordingProvider([
        TurnResult(text="Subagent finished report with context"),
    ])
    ctx = ToolContext(root=tmp_path)
    parent = Agent(prov, dict(SETTINGS), ctx, Permissions(), Events())
    # Populate parent with prior conversation history
    parent.messages = [
        {"role": "user", "content": "How do we implement authentication?"},
        {"role": "assistant", "content": "We can use JWT tokens stored in cookies."},
        {"role": "user", "content": "Let's focus on refresh tokens."},
    ]

    task_tool = TaskTool(parent=parent)
    report = task_tool.run({
        "description": "audit tokens",
        "prompt": "Inspect the token refresh logic",
        "capability": "read_only",
        "fork_context": True,
    }, ctx)

    assert report == "Subagent finished report with context"
    # Ensure provider received the subagent call with forked parent messages
    assert len(prov.calls) == 1
    sub_messages = prov.calls[0]["messages"]
    conv = [m for m in sub_messages if m.get("role") != "system"]

    # First messages should be the forked parent messages
    assert len(conv) == 4  # 3 parent messages + 1 subagent prompt
    assert conv[0]["content"] == "How do we implement authentication?"
    assert conv[1]["content"] == "We can use JWT tokens stored in cookies."
    assert conv[2]["content"] == "Let's focus on refresh tokens."
    assert "Inspect the token refresh logic" in conv[3]["content"]


def test_task_tool_does_not_fork_context_by_default(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    prov = RecordingProvider([
        TurnResult(text="Clean subagent report"),
    ])
    ctx = ToolContext(root=tmp_path)
    parent = Agent(prov, dict(SETTINGS), ctx, Permissions(), Events())
    parent.messages = [
        {"role": "user", "content": "Prior message"},
        {"role": "assistant", "content": "Prior response"},
    ]

    task_tool = TaskTool(parent=parent)
    report = task_tool.run({
        "description": "isolated task",
        "prompt": "Do fresh work without background context",
        "capability": "read_only",
        "fork_context": False,
    }, ctx)

    assert report == "Clean subagent report"
    assert len(prov.calls) == 1
    sub_messages = prov.calls[0]["messages"]
    conv = [m for m in sub_messages if m.get("role") != "system"]
    # Only the subagent prompt itself
    assert len(conv) == 1
    assert "Do fresh work without background context" in conv[0]["content"]


def test_task_tool_forks_context_when_agent_spec_specifies_fork_context(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    save_agent_definition(
        root=tmp_path,
        name="spec-forker",
        description="Auto forks context via spec",
        prompt="You are a spec-forker.",
        capability="read_only",
        color="purple",
        fork_context=True,
        scope="project",
    )

    prov = RecordingProvider([
        TurnResult(text="Spec-forked report"),
    ])
    ctx = ToolContext(root=tmp_path)
    parent = Agent(prov, dict(SETTINGS), ctx, Permissions(), Events())
    parent.messages = [
        {"role": "user", "content": "Important initial specification"},
        {"role": "assistant", "content": "Acknowledged specification"},
    ]

    task_tool = TaskTool(parent=parent)
    # Invoke with agent name, but do not pass fork_context in tool args
    report = task_tool.run({
        "description": "run spec agent",
        "prompt": "Check compliance",
        "agent": "spec-forker",
    }, ctx)

    assert report == "Spec-forked report"
    assert len(prov.calls) == 1
    sub_messages = prov.calls[0]["messages"]
    conv = [m for m in sub_messages if m.get("role") != "system"]
    # Context must be forked because spec.fork_context is True!
    assert len(conv) == 3
    assert conv[0]["content"] == "Important initial specification"
    assert conv[1]["content"] == "Acknowledged specification"
    assert "Check compliance" in conv[2]["content"]


def test_repl_agent_command_listing_and_fork(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    ctx = ToolContext(root=tmp_path)
    prov = RecordingProvider([])
    agent = Agent(prov, dict(SETTINGS), ctx, Permissions(), ReplEvents(ctx))
    repl = Repl(agent, SessionStore(tmp_path))

    # Test /agent or /agents registered
    cmd_names = repl.command_names()
    assert "agent" in cmd_names
    assert "agents" in cmd_names
    assert cmd_names["agent"] is cmd_names["agents"]

    # Test running /agent with no args (lists agents without error)
    repl.cmd_agents("")

    # Test /agent fork shortcut
    agent.messages = [
        {"role": "user", "content": "Optimize the database query pipeline"},
        {"role": "assistant", "content": "Working on index creation"},
    ]

    repl.cmd_agents("fork db-optimizer")
    forked_def = next((d for d in agent.agent_defs if d.name == "db-optimizer"), None)
    assert forked_def is not None
    assert forked_def.fork_context is True
    assert forked_def.color == "magenta"
    assert forked_def.capability == "edit"
    assert "Optimize the database query pipeline" in forked_def.prompt
    assert "Optimize the database query pipeline" in forked_def.description

    # Test inspecting created agent via /agent <name>
    repl.cmd_agents("db-optimizer")


def test_repl_agent_wizard_interactive_creation(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    ctx = ToolContext(root=tmp_path)
    prov = RecordingProvider([])
    agent = Agent(prov, dict(SETTINGS), ctx, Permissions(), ReplEvents(ctx))
    repl = Repl(agent, SessionStore(tmp_path))

    # Mock console.input answers in order:
    # name, description, prompt, capability, color, fork_context, scope
    inputs = [
        "wizard-specialist",
        "Expert wizard agent created through interactive REPL",
        "You follow wizard instructions carefully.",
        "2",        # edit
        "yellow",   # color
        "yes",      # fork_context
        "project",  # scope
    ]

    with patch("hubble.ui.console.input", side_effect=inputs):
        repl.cmd_agents("new")

    wiz_def = next((d for d in agent.agent_defs if d.name == "wizard-specialist"), None)
    assert wiz_def is not None
    assert wiz_def.capability == "edit"
    assert wiz_def.color == "yellow"
    assert wiz_def.fork_context is True
    assert "Expert wizard agent" in wiz_def.description
    assert "follow wizard instructions" in wiz_def.prompt
