import pytest
from pathlib import Path
from hubble.agent import Agent
from hubble.permissions import Permissions
from hubble.provider import ToolCall
from hubble.tools import ToolContext


class DummyEvents:
    def notice(self, *a, **k): pass
    def turn_start(self): pass
    def turn_end(self, *a): pass
    def tool_result(self, *a, **k): pass
    def text(self, *a): pass


def test_subagent_policy_auto(tmp_path):
    settings = {"model": "m", "subagent_policy": "auto"}
    agent = Agent(None, settings, ToolContext(root=tmp_path), Permissions(), DummyEvents())
    tool_names = [t.name for t in agent.active_tools()]
    assert "task" in tool_names


def test_subagent_policy_off(tmp_path):
    settings = {"model": "m", "subagent_policy": "off"}
    agent = Agent(None, settings, ToolContext(root=tmp_path), Permissions(), DummyEvents())
    agent.add_user_message("please spawn an agent to do this")
    tool_names = [t.name for t in agent.active_tools()]
    assert "task" not in tool_names

    res = agent._execute(ToolCall("1", "task", "{}"))
    assert "sub-agents are disabled under the current subagent_policy" in res


def test_subagent_policy_explicit(tmp_path):
    settings = {"model": "m", "subagent_policy": "explicit"}
    agent = Agent(None, settings, ToolContext(root=tmp_path), Permissions(), DummyEvents())
    
    # Casual prompt without agent keywords
    agent.add_user_message("how does authentication work in session.py?")
    assert "task" not in [t.name for t in agent.active_tools()]

    # With agent keyword
    agent.add_user_message("please delegate this to a subagent")
    assert "task" in [t.name for t in agent.active_tools()]

    # With 'agent' keyword
    agent.add_user_message("launch an agent for research")
    assert "task" in [t.name for t in agent.active_tools()]
