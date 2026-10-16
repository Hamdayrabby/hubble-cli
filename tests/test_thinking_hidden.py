import json
import pytest

from hubble.agent import Agent, Events
from hubble.permissions import Permissions
from hubble.provider import (
    TurnResult,
    ToolCall,
    strip_thinking,
    _StreamThinkingFilter,
)
from hubble.tools import ToolContext, default_tools
from hubble.ui import summarize_output


def test_strip_thinking_removes_closed_and_unclosed_tags():
    assert strip_thinking("<think>internal thought</think>Here is the answer.") == "Here is the answer."
    assert strip_thinking("<thought>\nMulti\nline\nthoughts\n</thought>\n\nDone.") == "Done."
    assert strip_thinking("<reasoning>reasoning steps</reasoning>42") == "42"
    # Unclosed tag at the end (stream cut off mid-thought)
    assert strip_thinking("<think>cut off mid thought") == ""
    # Mixed content
    assert strip_thinking("Prefix <think>middle thought</think> suffix") == "Prefix  suffix"
    # Empty or None
    assert strip_thinking("") == ""


def test_stream_thinking_filter_basic():
    text_out = []
    reasoning_out = []
    text_parts = []
    reasoning_parts = []

    f = _StreamThinkingFilter(
        on_text=text_out.append,
        on_reasoning=reasoning_out.append,
        text_parts=text_parts,
        reasoning_parts=reasoning_parts,
    )

    f.feed("<think>I am calculating 2+2</think>The answer is 4.")
    f.flush()

    assert "".join(reasoning_parts) == "I am calculating 2+2"
    assert "".join(reasoning_out) == "I am calculating 2+2"
    assert "".join(text_parts) == "The answer is 4."
    assert "".join(text_out) == "The answer is 4."


def test_stream_thinking_filter_split_across_chunks():
    text_out = []
    reasoning_out = []
    text_parts = []
    reasoning_parts = []

    f = _StreamThinkingFilter(
        on_text=text_out.append,
        on_reasoning=reasoning_out.append,
        text_parts=text_parts,
        reasoning_parts=reasoning_parts,
    )

    # Chunks split across open tag <think>
    f.feed("<th")
    f.feed("ink>First plan.")
    f.feed(" Second plan.")
    # Split across close tag </think>
    f.feed(" Done planning</")
    f.feed("think>Let's execute!")
    f.flush()

    assert "".join(reasoning_parts) == "First plan. Second plan. Done planning"
    assert "".join(text_parts) == "Let's execute!"


def test_stream_thinking_filter_without_thinking():
    text_out = []
    reasoning_out = []
    text_parts = []
    reasoning_parts = []

    f = _StreamThinkingFilter(
        on_text=text_out.append,
        on_reasoning=reasoning_out.append,
        text_parts=text_parts,
        reasoning_parts=reasoning_parts,
    )

    f.feed("Plain response with <brackets> that are not thinking tags.")
    f.flush()

    assert "".join(reasoning_parts) == ""
    assert "".join(text_parts) == "Plain response with <brackets> that are not thinking tags."


class MockProvider:
    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, model, messages, tools=None, on_text=None, on_reasoning=None, **kw):
        turn = self.turns.pop(0)
        if on_text and turn.text:
            on_text(turn.text)
        return turn

    def complete(self, model, messages, **kw):
        return "summary"


def test_agent_hides_chain_of_thinking_on_task_completion(tmp_path):
    ctx = ToolContext(root=tmp_path)
    ev = Events()

    # Model outputs <think> tags in its turn
    raw_response = "<think>\n1. Read files\n2. Realized task is done.\n</think>\nTask complete: all changes verified."
    provider = MockProvider([TurnResult(text=raw_response)])

    agent = Agent(provider, {"model": "m", "max_turns": 3}, ctx, Permissions("default"), ev)
    result = agent.run("Do the task")

    assert "<think>" not in result
    assert "Realized task is done" not in result
    assert result == "Task complete: all changes verified."
    # The message history also has the clean assistant response
    assert "<think>" not in agent.messages[-1]["content"]
    assert agent.messages[-1]["content"] == "Task complete: all changes verified."


def test_task_tool_summary_hides_chain_of_thought():
    class DummyTool:
        name = "task"
    
    output_with_thought = "<think>\nThinking about reading files...\nMore thinking...\n</think>\nFound the answer in config.py."
    headline, extra = summarize_output(DummyTool(), output_with_thought, False)

    assert "Report ready" in headline
    # Extra lines must NOT show thinking
    for line in extra:
        assert "<think>" not in line
        assert "Thinking about" not in line
    assert any("Found the answer" in line for line in extra)
