import json

from hubble.agent import Agent, Events
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.tools import ToolContext

BASE = {"model": "m", "max_turns": 6, "max_tokens": 10, "context_window": 100000, "auto_compact_ratio": 0,
        "persona": "code", "web_tools": False, "subagent_max_turns": 5}


class Reader:
    """Sub-agent keeps reading forever (like a model that never stops); answers the final ask
    with `final_text`. Records what it was told."""

    def __init__(self, parent_turns, final_text="Found: the editor uses a timeline store."):
        self.parent = list(parent_turns)
        self.final_text = final_text
        self.sub_calls = 0
        self.seen_notes = []

    def stream(self, model, messages, **kw):
        if "sub-agent" not in messages[0]["content"]:
            return self.parent.pop(0)
        self.sub_calls += 1
        last = messages[-1].get("content") or ""
        if "Hubble:" in last:
            self.seen_notes.append(last[last.index("[Hubble:"):])
        if "step limit reached" in last:
            return TurnResult(text=self.final_text)
        return TurnResult(tool_calls=[ToolCall(f"r{self.sub_calls}", "read_file",
                                               json.dumps({"path": "a.txt"}))])


class Board(Events):
    def __init__(self):
        self.ends = []

    def subagent_end(self, key, status, detail=""):
        self.ends.append((status, detail))


def run(tmp_path, provider):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    board = Board()
    agent = Agent(provider, dict(BASE), ToolContext(root=tmp_path), Permissions(), board)
    agent.run("research")
    tool_out = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    return board, tool_out


def test_out_of_steps_subagent_still_writes_a_report(tmp_path):
    p = Reader([TurnResult(tool_calls=[ToolCall("t", "task", json.dumps({"description": "dig", "prompt": "p"}))]),
                TurnResult(text="done")])
    board, report = run(tmp_path, p)
    assert "timeline store" in report and "ran out of steps" in report
    assert any("3 steps left" in n for n in p.seen_notes)          # warned before the limit
    assert p.sub_calls == 5 + 1                                    # its steps plus one report call
    assert board.ends[-1][0] == "partial"                          # honest status, not a plain ✔


def test_model_that_never_reports_hands_back_what_it_read(tmp_path):
    p = Reader([TurnResult(tool_calls=[ToolCall("t", "task", json.dumps({"description": "dig", "prompt": "p"}))]),
                TurnResult(text="done")], final_text="")
    board, report = run(tmp_path, p)
    assert "ran out of steps before writing a report" in report and "read_file(a.txt)" in report
    assert "instead of re-running" in report                       # steer the parent away from a retry loop


def test_main_agent_limit_unchanged(tmp_path):
    class Forever:
        def stream(self, model, messages, **kw):
            return TurnResult(tool_calls=[ToolCall("x", "read_file", json.dumps({"path": "a.txt"}))])

    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    notices = []

    class E(Events):
        def notice(self, m, level="info"):
            notices.append(m)

    agent = Agent(Forever(), {**BASE, "max_turns": 3}, ToolContext(root=tmp_path), Permissions(), E())
    assert agent.run("go") == ""
    assert agent.last_stats.hit_step_limit and any("max_turns" in n for n in notices)
