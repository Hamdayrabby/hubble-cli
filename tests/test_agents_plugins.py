import json

import hubble.plugins as plugins
import hubble.settings as settings_mod
import hubble.skills as skills_mod
import hubble.subagents as subagents
from hubble.agent import Agent, Events, TaskTool
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.tools import ToolContext

SETTINGS = {"model": "m", "max_turns": 6, "max_tokens": 100, "context_window": 100000,
            "auto_compact_ratio": 0, "persona": "code", "web_tools": False}


class RecordingProvider:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def stream(self, model, messages, tools=None, on_text=None, **kw):
        self.calls.append({"model": model, "system": messages[0]["content"],
                           "tools": [t["function"]["name"] for t in tools or []]})
        return self.turns.pop(0)


def isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for mod in (plugins, subagents, skills_mod, settings_mod):
        monkeypatch.setattr(mod, "HOME_DIR", home)
    return home


def write_agent(root, name, front, body="You review code for security bugs."):
    d = root / ".hubble" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\n{front}\n---\n{body}\n", encoding="utf-8")


def test_discover_agents_parses_frontmatter(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    write_agent(tmp_path, "sec", "name: sec\ndescription: Security review\ntools: read_file, grep\nmodel: big")
    write_agent(tmp_path, "fixer", "name: fixer\ndescription: Fixes things\ntools: read_file, edit_file")
    defs = {a.name: a for a in subagents.discover_agents(tmp_path)}
    assert defs["sec"].tools == ["read_file", "grep"] and defs["sec"].model == "big"
    assert defs["sec"].capability == "read_only"
    assert defs["fixer"].capability == "edit"  # inferred from its edit tool


def test_task_with_named_agent_uses_its_prompt_tools_and_model(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    write_agent(tmp_path, "sec", "name: sec\ndescription: Security review\ntools: read_file, grep\nmodel: big")
    provider = RecordingProvider([
        TurnResult(tool_calls=[ToolCall("c1", "task", json.dumps({"description": "audit", "prompt": "look",
                                                                  "agent": "sec"}))]),
        TurnResult(text="no bugs found"),
        TurnResult(text="done"),
    ])
    agent = Agent(provider, dict(SETTINGS), ToolContext(root=tmp_path), Permissions(), Events())
    task_schema = next(t for t in agent.tools if t.name == "task").schema()["function"]
    assert "sec: Security review" in task_schema["description"]
    assert task_schema["parameters"]["properties"]["agent"]["enum"] == ["sec"]
    agent.run("audit the code")
    sub = provider.calls[1]
    assert sub["model"] == "big"
    assert sorted(sub["tools"]) == ["grep", "read_file"]
    assert "security bugs" in sub["system"]


def test_read_only_agent_never_gets_edit_tools(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    write_agent(tmp_path, "sneaky", "name: sneaky\ndescription: x\ncapability: read_only\ntools: read_file, write_file")
    provider = RecordingProvider([
        TurnResult(tool_calls=[ToolCall("c1", "task", json.dumps({"description": "a", "prompt": "p",
                                                                  "agent": "sneaky"}))]),
        TurnResult(text="r"), TurnResult(text="done"),
    ])
    agent = Agent(provider, dict(SETTINGS), ToolContext(root=tmp_path), Permissions(), Events())
    agent.run("go")
    assert provider.calls[1]["tools"] == ["read_file"]


def test_task_schema_without_agents_has_no_agent_param():
    assert "agent" not in TaskTool(parent=None).parameters["properties"]


def make_plugin(src, hooks=True):
    src.mkdir(parents=True)
    manifest = {"name": "demo", "version": "1.2.0", "description": "Demo plugin"}
    if hooks:
        manifest["hooks"] = {"PreToolUse": [{"matcher": "shell", "command": "python ${PLUGIN_DIR}/check.py"}]}
        manifest["mcp_servers"] = {"srv": {"command": ["python", "${PLUGIN_DIR}/srv.py"]}}
    (src / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (src / "commands").mkdir()
    (src / "commands" / "hello.md").write_text("Say hello to $ARGUMENTS", encoding="utf-8")
    (src / "skills").mkdir()
    (src / "skills" / "lint.md").write_text("---\nname: lint\ndescription: Lint it\n---\nRun ruff.", encoding="utf-8")
    (src / "agents").mkdir()
    (src / "agents" / "helper.md").write_text("---\nname: helper\ndescription: Helps\n---\nHelp.", encoding="utf-8")


def test_plugin_install_bundles_everything(tmp_path, monkeypatch):
    home = isolate_home(tmp_path, monkeypatch)
    root = tmp_path / "proj"
    root.mkdir()
    make_plugin(tmp_path / "src")
    info = plugins.install(str(tmp_path / "src"), root)
    assert info["name"] == "demo" and (home / "plugins" / "demo" / "plugin.json").is_file()

    assert [s.name for s in skills_mod.discover_skills(root)] == ["lint"]
    assert [a.name for a in subagents.discover_agents(root)] == ["helper"]
    hooks, servers = plugins.plugin_settings(root, trusted=False)  # user plugin: trust not needed
    cmd = hooks["PreToolUse"][0]["command"]
    assert "${PLUGIN_DIR}" not in cmd and str(home / "plugins" / "demo") in cmd
    assert "demo-srv" in servers

    loaded = settings_mod.load_settings(root, trusted=False)
    assert loaded["hooks"]["PreToolUse"][0]["matcher"] == "shell"
    assert "demo-srv" in loaded["mcp_servers"]


def test_project_plugin_hooks_need_trust(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    root = tmp_path / "proj"
    root.mkdir()
    make_plugin(tmp_path / "src")
    plugins.install(str(tmp_path / "src"), root, scope="project")
    assert plugins.plugin_settings(root, trusted=False) == ({}, {})
    hooks, servers = plugins.plugin_settings(root, trusted=True)
    assert hooks and servers
    # Skills/agents/commands are plain instructions, like .hubble/skills, and load either way.
    assert [s.name for s in skills_mod.discover_skills(root)] == ["lint"]


def test_disable_and_remove_plugin(tmp_path, monkeypatch):
    isolate_home(tmp_path, monkeypatch)
    root = tmp_path / "proj"
    root.mkdir()
    make_plugin(tmp_path / "src", hooks=False)
    plugins.install(str(tmp_path / "src"), root)
    plugins.set_enabled("demo", False)
    assert plugins.plugin_dirs(root) == []
    assert plugins.installed_plugins(root, include_disabled=True)[0]["enabled"] is False
    plugins.set_enabled("demo", True)
    assert plugins.remove("demo", root) and plugins.installed_plugins(root) == []
