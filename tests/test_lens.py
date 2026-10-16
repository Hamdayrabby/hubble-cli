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


class DummyEvents:
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
