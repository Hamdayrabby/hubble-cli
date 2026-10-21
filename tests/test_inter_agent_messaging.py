import json

import pytest

from hubble.agent import Agent, Events
from hubble.messaging import MessageBus
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.tools import SendMessage, ToolContext

BASE = {
    "model": "m",
    "max_turns": 6,
    "max_tokens": 100,
    "context_window": 100000,
    "auto_compact_ratio": 0,
    "persona": "code",
    "web_tools": False,
}


def test_message_bus_registration_and_mailboxes():
    bus = MessageBus()
    bus.register_agent("main", role="coordinator")
    bus.register_agent("reviewer", role="code-reviewer")

    assert bus.is_registered("main")
    assert bus.is_registered("reviewer")
    assert not bus.is_registered("unknown")

    agents = bus.list_active_agents()
    agent_ids = {a["id"] for a in agents}
    assert agent_ids == {"main", "reviewer"}

    bus.unregister_agent("reviewer")
    assert not bus.is_registered("reviewer")


def test_message_bus_send_and_receive():
    bus = MessageBus()
    bus.register_agent("agent-1")
    bus.register_agent("agent-2")

    msg = bus.send(sender="agent-1", recipient="agent-2", content="Need review on PR #42")
    assert msg.sender == "agent-1"
    assert msg.recipient == "agent-2"
    assert msg.content == "Need review on PR #42"
    assert not msg.read

    assert bus.has_unread("agent-2")
    assert not bus.has_unread("agent-1")

    # Peek unread messages without marking read
    peeked = bus.peek("agent-2")
    assert len(peeked) == 1
    assert not peeked[0].read

    # Receive messages
    received = bus.receive("agent-2")
    assert len(received) == 1
    assert received[0].content == "Need review on PR #42"
    assert received[0].read

    # Second receive should be empty
    assert len(bus.receive("agent-2")) == 0


def test_message_bus_broadcast():
    bus = MessageBus()
    bus.register_agent("coord")
    bus.register_agent("sub-1")
    bus.register_agent("sub-2")

    bus.send(sender="coord", recipient="broadcast", content="New test suite released")

    # Both sub-agents should have received the message, but not the sender
    assert not bus.has_unread("coord")
    assert bus.has_unread("sub-1")
    assert bus.has_unread("sub-2")

    sub1_msgs = bus.receive("sub-1")
    sub2_msgs = bus.receive("sub-2")
    assert len(sub1_msgs) == 1 and sub1_msgs[0].content == "New test suite released"
    assert len(sub2_msgs) == 1 and sub2_msgs[0].content == "New test suite released"


def test_send_message_tool_execution(tmp_path):
    bus = MessageBus()
    ctx = ToolContext(root=tmp_path, message_bus=bus, agent_id="tester")

    tool = SendMessage()
    res = tool.run({"recipient": "reviewer", "message": "Tests passed, ready for sign-off"}, ctx)
    assert "Message delivered to 'reviewer'" in res

    unread = bus.receive("reviewer")
    assert len(unread) == 1
    assert unread[0].sender == "tester"
    assert unread[0].content == "Tests passed, ready for sign-off"


def test_agent_reactive_message_injection(tmp_path):
    bus = MessageBus()
    ctx = ToolContext(root=tmp_path, message_bus=bus, agent_id="main")

    # Push a message into main's mailbox
    bus.send(sender="task-1", recipient="main", content="Found potential SQL injection in auth.py:42")

    class MockProvider:
        def __init__(self):
            self.calls = []

        def stream(self, model, messages, **kw):
            self.calls.append(list(messages))
            return TurnResult(text="Noted the security finding.")

    provider = MockProvider()
    agent = Agent(provider, dict(BASE), ctx, Permissions(), Events())
    agent.run("check system status")

    # Verify that the incoming message was reactively injected into context
    first_call_msgs = provider.calls[0]
    injected = any("<incoming-messages>" in str(m.get("content", "")) and "SQL injection in auth.py:42" in str(m.get("content", ""))
                   for m in first_call_msgs)
    assert injected


def test_subagent_messaging_parent(tmp_path):
    bus = MessageBus()
    ctx = ToolContext(root=tmp_path, message_bus=bus, agent_id="main")

    # Parent spawns a subagent, subagent calls send_message to 'parent'
    class ScriptedSubProvider:
        def __init__(self):
            self.parent_turns = [
                TurnResult(tool_calls=[ToolCall("t1", "task", json.dumps({
                    "description": "audit auth",
                    "prompt": "audit authentication logic",
                    "capability": "read_only",
                }))]),
                TurnResult(text="Audit complete."),
            ]

        def stream(self, model, messages, **kw):
            content = str(messages[0].get("content", ""))
            if "sub-agent" in content or "task" in content:
                if len(messages) <= 2:
                    return TurnResult(tool_calls=[ToolCall("s1", "send_message", json.dumps({
                        "recipient": "parent",
                        "message": "Auth audit progress: found 2 exposed endpoints",
                    }))])
                return TurnResult(text="Subagent finished audit.")
            return self.parent_turns.pop(0)

    provider = ScriptedSubProvider()
    agent = Agent(provider, dict(BASE), ctx, Permissions(), Events())
    agent.run("run audit")

    # Parent agent should have received and injected the subagent's message into its conversation context
    injected = any("<incoming-messages>" in str(m.get("content", "")) and "found 2 exposed endpoints" in str(m.get("content", ""))
                   for m in agent.messages)
    assert injected
