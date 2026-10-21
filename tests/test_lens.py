import pytest
from pathlib import Path
from hubble.prompts import (
    find_lens_files,
    load_lens,
    build_system_prompt,
    LENS_TEMPLATE,
)
from hubble.agent import Agent, Events
from hubble.permissions import Permissions
from hubble.tools import ToolContext


class DummyEvents(Events):
    def notice(self, *a, **k): pass
    def turn_start(self): pass
    def turn_end(self, *a): pass
    def tool_result(self, *a, **k): pass
    def text(self, *a): pass


def test_lens_file_discovery_and_loading(tmp_path):
    lens_file = tmp_path / "LENS.md"
    lens_file.write_text(
        "# My Lens\n## UI\n- Always use Tailwind CSS with zinc palette and dark mode by default.",
        encoding="utf-8"
    )

    found = find_lens_files(tmp_path)
    assert lens_file in found

    loaded = load_lens(tmp_path)
    assert len(loaded) == 1
    assert loaded[0][0] == lens_file
    assert "Tailwind CSS with zinc palette" in loaded[0][1]


def test_lens_fallback_to_style_and_taste(tmp_path):
    # Tests that STYLE.md or taste.md are also discovered seamlessly
    style_file = tmp_path / "STYLE.md"
    style_file.write_text("- Early returns.\n- No any in TypeScript.", encoding="utf-8")

    found = find_lens_files(tmp_path)
    assert style_file in found

    loaded = load_lens(tmp_path)
    assert len(loaded) == 1
    assert "Early returns" in loaded[0][1]


def test_lens_injected_in_system_prompt(tmp_path):
    lens_file = tmp_path / "LENS.md"
    lens_file.write_text("- Prefer early returns and guard clauses.\n- Avoid any type in TypeScript.", encoding="utf-8")

    lens_data = load_lens(tmp_path)
    prompt = build_system_prompt(
        root=tmp_path,
        persona="code",
        shell="powershell.exe",
        model="test-model",
        pinned={},
        memory=[],
        mode="default",
        lens=lens_data,
    )

    assert "# Developer Lens & Design Language from" in prompt
    assert "Prefer early returns and guard clauses" in prompt
    assert "Avoid any type in TypeScript" in prompt


def test_agent_automatically_loads_lens(tmp_path):
    lens_file = tmp_path / "LENS.md"
    lens_file.write_text("Lens rule: Never use eval() or inline styles.", encoding="utf-8")

    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(), DummyEvents())

    assert len(agent.lens) == 1
    assert "Never use eval() or inline styles" in agent.lens[0][1]
    prompt = agent.system_prompt()
    assert "Never use eval() or inline styles" in prompt


def test_agent_reload_lens(tmp_path):
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(), DummyEvents())
    assert len(agent.lens) == 0

    lens_file = tmp_path / "LENS.md"
    lens_file.write_text("New Lens: Strict functional programming.", encoding="utf-8")
    agent.reload_memory()

    assert len(agent.lens) == 1
    assert "Strict functional programming" in agent.lens[0][1]


class RecordingEvents(DummyEvents):
    def __init__(self):
        self.notices = []

    def notice(self, message: str, level: str = "info"):
        self.notices.append((message, level))


def test_lens_auto_update_on_file_modification(tmp_path):
    lens_file = tmp_path / "LENS.md"
    lens_file.write_text("- Initial rule: Concise code.", encoding="utf-8")

    events = RecordingEvents()
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(), events)

    assert len(agent.lens) == 1
    assert "Initial rule: Concise code" in agent.lens[0][1]
    assert len(events.notices) == 0

    # Without modifying file, check_and_update_lens returns False
    assert agent.check_and_update_lens() is False
    assert len(events.notices) == 0

    # Modify lens file
    lens_file.write_text("- Updated rule: Strict typing and dark mode.", encoding="utf-8")
    assert agent.check_and_update_lens() is True
    assert len(agent.lens) == 1
    assert "Strict typing and dark mode" in agent.lens[0][1]
    assert agent.taste == agent.lens
    assert ("Developer lens updated (1 file active)", "info") in events.notices


def test_lens_auto_update_on_file_creation_and_removal(tmp_path):
    events = RecordingEvents()
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(), events)
    assert len(agent.lens) == 0

    # Create lens file
    lens_file = tmp_path / "LENS.md"
    lens_file.write_text("- Design tokens in CSS.", encoding="utf-8")

    assert agent.check_and_update_lens() is True
    assert len(agent.lens) == 1
    assert "Design tokens in CSS" in agent.lens[0][1]
    assert ("Developer lens updated (1 file active)", "info") in events.notices

    # Remove lens file
    lens_file.unlink()
    events.notices.clear()

    assert agent.check_and_update_lens() is True
    assert len(agent.lens) == 0
    assert ("Developer lens updated (0 files active)", "info") in events.notices


def test_reload_memory_emits_notice_when_lens_changed(tmp_path):
    events = RecordingEvents()
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(), events)
    assert len(agent.lens) == 0

    lens_file = tmp_path / "LENS.md"
    lens_file.write_text("- Early return guard clauses.", encoding="utf-8")

    agent.reload_memory()
    assert len(agent.lens) == 1
    assert "Early return guard clauses" in agent.lens[0][1]
    assert ("Developer lens updated (1 file active)", "info") in events.notices

    # Subsequent reload_memory with no change emits no notice
    events.notices.clear()
    agent.reload_memory()
    assert len(events.notices) == 0


def test_tool_execution_triggers_lens_update(tmp_path):
    import json
    from hubble.provider import ToolCall

    events = RecordingEvents()
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(mode="yolo"), events)
    assert len(agent.lens) == 0

    call = ToolCall(
        id="call_1",
        name="write_file",
        arguments=json.dumps({"path": "LENS.md", "content": "- Added via write_file tool."})
    )
    output = agent._execute(call)
    assert "Error" not in output
    assert len(agent.lens) == 1
    assert "Added via write_file tool" in agent.lens[0][1]
    assert ("Developer lens updated (1 file active)", "info") in events.notices


def test_repl_cmd_lens_init_and_add_confirmation(tmp_path):
    from unittest.mock import MagicMock
    from hubble.repl import Repl

    events = RecordingEvents()
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(mode="yolo"), events)
    repl = Repl(agent, MagicMock())

    # Run /lens init
    repl.cmd_lens("init")
    assert (tmp_path / "LENS.md").exists()
    assert len(agent.lens) == 1
    assert any("Developer lens updated" in msg for msg, _ in events.notices)

    # Run /lens add
    events.notices.clear()
    repl.cmd_lens("add Prefer CSS modules")
    assert "Prefer CSS modules" in (tmp_path / "LENS.md").read_text(encoding="utf-8")
    assert any("Prefer CSS modules" in text for _, text in agent.lens)
    assert any("Developer lens updated" in msg for msg, _ in events.notices)

