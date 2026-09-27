import json
from pathlib import Path

import pytest

from hubble.agent import Agent
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.skills import (WriteSkillTool, discover_skills, skills_prompt_block)
from hubble.tools import ToolContext, run_tool
from hubble.ui import ReplEvents

SETTINGS = {"model": "m", "max_turns": 5, "max_tokens": 100, "context_window": 100000,
            "auto_compact_ratio": 0.8, "persona": "code"}


def write_skill(base: Path, name: str, description: str, body: str = "Do the thing.", frontmatter=True):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    if frontmatter:
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
                                    encoding="utf-8")
    else:
        (base / f"{name}.md").write_text(body, encoding="utf-8")


def test_discover_project_and_user_skills(tmp_path, monkeypatch):
    import hubble.skills as sk
    home = tmp_path / "home"
    monkeypatch.setattr(sk, "HOME_DIR", home)
    root = tmp_path / "project"
    root.mkdir()
    write_skill(home / "skills", "deploy", "Deploy the app. Use when asked to ship or release.")
    write_skill(root / ".hubble" / "skills", "test-suite", "Run tests and summarize failures.")
    skills = discover_skills(root)
    names = {s.name: s for s in skills}
    assert set(names) == {"deploy", "test-suite"}
    assert names["deploy"].scope == "user" and names["test-suite"].scope == "project"
    assert "Do the thing." in names["deploy"].body()


def test_project_skill_overrides_user_skill_of_same_name(tmp_path, monkeypatch):
    import hubble.skills as sk
    home = tmp_path / "home"
    monkeypatch.setattr(sk, "HOME_DIR", home)
    root = tmp_path / "project"
    write_skill(home / "skills", "deploy", "user version", body="user body")
    write_skill(root / ".hubble" / "skills", "deploy", "project version", body="project body")
    skills = discover_skills(root)
    assert len(skills) == 1
    assert skills[0].scope == "project" and skills[0].description == "project version"


def test_skill_without_frontmatter_falls_back_to_first_line(tmp_path, monkeypatch):
    import hubble.skills as sk
    monkeypatch.setattr(sk, "HOME_DIR", tmp_path / "home")
    root = tmp_path / "project"
    write_skill(root / ".hubble" / "skills", "quick", "unused", body="# Quick check\nsteps...", frontmatter=False)
    skills = discover_skills(root)
    assert skills[0].name == "quick" and skills[0].description == "Quick check"


def test_skills_prompt_block_is_names_and_descriptions_only(tmp_path, monkeypatch):
    import hubble.skills as sk
    monkeypatch.setattr(sk, "HOME_DIR", tmp_path / "home")
    root = tmp_path / "project"
    write_skill(root / ".hubble" / "skills", "deploy", "Deploy the app.", body="SECRET STEP: rm -rf /")
    skills = discover_skills(root)
    block = skills_prompt_block(skills)
    assert "deploy: Deploy the app." in block
    assert "SECRET STEP" not in block  # body loads only via the skill tool, not eagerly


def test_write_skill_tool_creates_and_updates(tmp_path, monkeypatch):
    import hubble.skills as sk
    monkeypatch.setattr(sk, "HOME_DIR", tmp_path / "home")
    ctx = ToolContext(root=tmp_path / "project")
    out, err = run_tool(WriteSkillTool(), {"name": "release", "description": "Cut a release.",
                                          "body": "1. bump version\n2. tag"}, ctx)
    assert not err, out
    path = ctx.root / ".hubble" / "skills" / "release" / "SKILL.md"
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert "description: Cut a release." in content and "1. bump version" in content

    # A different scope is a different file, so it is created, not updated.
    out, err = run_tool(WriteSkillTool(), {"name": "release", "description": "Cut a release (v2).",
                                          "body": "steps", "scope": "user"}, ctx)
    assert not err and "Saved" in out
    user_path = tmp_path / "home" / "skills" / "release" / "SKILL.md"
    assert user_path.is_file() and "Cut a release (v2)." in user_path.read_text(encoding="utf-8")

    # Writing the same project skill again updates it in place.
    out, err = run_tool(WriteSkillTool(), {"name": "release", "description": "Cut a release (v3).",
                                          "body": "steps"}, ctx)
    assert not err and "Updated" in out
    assert "Cut a release (v3)." in path.read_text(encoding="utf-8")


def test_write_skill_tool_rejects_bad_name(tmp_path):
    ctx = ToolContext(root=tmp_path)
    out, err = run_tool(WriteSkillTool(), {"name": "Bad Name!", "description": "x", "body": "y"}, ctx)
    assert err and "lowercase" in out


def call(name, args, cid="c1"):
    return ToolCall(cid, name, json.dumps(args))


def test_agent_loads_skill_and_uses_prompt_block(tmp_path, monkeypatch):
    import hubble.skills as sk
    monkeypatch.setattr(sk, "HOME_DIR", tmp_path / "home")
    write_skill(tmp_path / ".hubble" / "skills", "onboarding", "Explain the repo layout to new contributors.")
    ctx = ToolContext(root=tmp_path)
    agent = Agent(_FakeProvider([TurnResult(text="done")]), dict(SETTINGS), ctx, Permissions(), ReplEvents(ctx))
    assert [s.name for s in agent.skills] == ["onboarding"]
    assert "onboarding" in agent.system_prompt()
    assert "skill" in agent.tools_by_name


class _FakeProvider:
    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, model, messages, tools=None, on_text=None, **kw):
        turn = self.turns.pop(0)
        if on_text and turn.text:
            on_text(turn.text)
        return turn

    def complete(self, model, messages, **kw):
        return "SUMMARY"


def test_agent_calls_skill_tool_and_gets_full_body(tmp_path, monkeypatch):
    import hubble.skills as sk
    monkeypatch.setattr(sk, "HOME_DIR", tmp_path / "home")
    write_skill(tmp_path / ".hubble" / "skills", "release", "Cut a release.", body="1. bump\n2. tag\n3. push")
    ctx = ToolContext(root=tmp_path)
    agent = Agent(_FakeProvider([
        TurnResult(tool_calls=[call("skill", {"name": "release"})]),
        TurnResult(text="Following the release skill now."),
    ]), dict(SETTINGS), ctx, Permissions(), ReplEvents(ctx))
    agent.run("cut a release")
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "1. bump" in tool_msg["content"] and "3. push" in tool_msg["content"]


def test_agent_writes_skill_with_approval_and_reloads(tmp_path, monkeypatch):
    import hubble.skills as sk
    monkeypatch.setattr(sk, "HOME_DIR", tmp_path / "home")
    ctx = ToolContext(root=tmp_path)

    class Events(ReplEvents):
        def ask(self, tool, args, preview):
            return "yes", ""

    agent = Agent(_FakeProvider([
        TurnResult(tool_calls=[call("write_skill", {
            "name": "debug-flow", "description": "Debug a failing test.", "body": "Reproduce, then fix."})]),
        TurnResult(text="Saved."),
    ]), dict(SETTINGS), ctx, Permissions("default"), Events(ctx))
    assert agent.skills == []
    agent.run("save this as a skill")
    assert (tmp_path / ".hubble" / "skills" / "debug-flow" / "SKILL.md").is_file()
    assert [s.name for s in agent.skills] == ["debug-flow"]
    assert "debug-flow" in agent.system_prompt()


def test_agent_write_skill_denied_does_not_reload(tmp_path, monkeypatch):
    import hubble.skills as sk
    monkeypatch.setattr(sk, "HOME_DIR", tmp_path / "home")
    ctx = ToolContext(root=tmp_path)

    class Events(ReplEvents):
        def ask(self, tool, args, preview):
            return "no", "not now"

    agent = Agent(_FakeProvider([
        TurnResult(tool_calls=[call("write_skill", {
            "name": "nope", "description": "x", "body": "y"})]),
        TurnResult(text="OK, skipping."),
    ]), dict(SETTINGS), ctx, Permissions("default"), Events(ctx))
    agent.run("save it")
    assert agent.skills == []
    assert not (tmp_path / ".hubble" / "skills" / "nope").exists()
