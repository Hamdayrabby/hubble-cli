import json

from hubble.agent import Agent, Events, _Progress
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.tools import ToolContext

BASE = {"model": "m", "max_turns": 6, "max_tokens": 10, "context_window": 100000, "auto_compact_ratio": 0,
        "persona": "code", "web_tools": False, "subagent_max_turns": 5}


def read(i, path=None):
    return ToolCall(f"r{i}", "read_file", json.dumps({"path": path or f"f{i}.txt"}))


class Sub:
    """Parent turns scripted; the sub-agent's steps come from `step(n)`; the wrap-up ask gets
    `final_text`. Records the Hubble notes it was shown."""

    def __init__(self, parent_turns, step, final_text="Found: the editor uses a timeline store."):
        self.parent, self.step, self.final_text = list(parent_turns), step, final_text
        self.sub_calls = 0
        self.notes = []

    def stream(self, model, messages, **kw):
        if "sub-agent" not in messages[0]["content"]:
            return self.parent.pop(0)
        self.sub_calls += 1
        last = messages[-1].get("content") or ""
        if "[Hubble:" in last:
            self.notes.append(last[last.index("[Hubble:"):])
        if "Write your final report now" in last:
            return TurnResult(text=self.final_text)
        return TurnResult(tool_calls=[self.step(self.sub_calls)])


class Board(Events):
    def __init__(self):
        self.ends, self.notices = [], []

    def subagent_end(self, key, status, detail=""):
        self.ends.append((status, detail))

    def notice(self, m, level="info"):
        self.notices.append(m)


PARENT = [TurnResult(tool_calls=[ToolCall("t", "task", json.dumps({"description": "dig", "prompt": "p"}))]),
          TurnResult(text="done")]


def run(tmp_path, provider, settings=None):
    board = Board()
    agent = Agent(provider, {**BASE, **(settings or {})}, ToolContext(root=tmp_path), Permissions(), board)
    agent.run("research")
    tool_out = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    return board, tool_out


def test_backstop_limit_still_gets_a_report(tmp_path):
    p = Sub(PARENT, step=lambda n: read(n))          # always something new: only the backstop stops it
    board, report = run(tmp_path, p)
    assert "timeline store" in report and "step limit reached (5 steps)" in report
    assert any("3 steps left" in n for n in p.notes)
    assert board.ends[-1][0] == "partial"


def test_repeating_the_same_call_wraps_up_early(tmp_path):
    p = Sub(PARENT, step=lambda n: read(n, "same.txt"))
    board, report = run(tmp_path, p, {"subagent_max_turns": 150})
    assert p.sub_calls == 3 + 1                       # three identical reads, then the report ask
    assert "repeating the same read_file call" in report and "timeline store" in report


def test_token_budget_wraps_up(tmp_path):
    class Heavy(Sub):
        def stream(self, model, messages, **kw):
            r = super().stream(model, messages, **kw)
            r.usage = {"prompt_tokens": 400, "completion_tokens": 100}
            return r
    p = Heavy(PARENT, step=lambda n: read(n))
    board, report = run(tmp_path, p, {"subagent_max_turns": 150, "subagent_token_budget": 1000})
    assert "token budget reached (1,000 tokens)" in report


def test_model_that_never_reports_hands_back_what_it_read(tmp_path):
    p = Sub(PARENT, step=lambda n: read(n), final_text="")
    board, report = run(tmp_path, p)
    assert "before writing a report" in report and "read_file(f1.txt)" in report
    assert "instead of re-running" in report


def test_progress_resets_after_changes():
    pr = _Progress()
    for _ in range(3):  # read, edit, read, edit, read: verifying after each change is progress
        pr.note([ToolCall("a", "read_file", json.dumps({"path": "x.py"}))])
        pr.note([ToolCall("b", "edit_file", json.dumps({"path": "x.py", "old_string": "a", "new_string": "b"}))])
    assert pr.stalled() is None
    for _ in range(3):
        pr.note([ToolCall("c", "grep", json.dumps({"pattern": "foo"}))])
    assert "repeating the same grep" in pr.stalled()


def test_idle_steps_detected():
    pr = _Progress()
    calls = [ToolCall(str(i), "read_file", json.dumps({"path": f"{i}.py"})) for i in range(2)]
    pr.note(calls)
    for i in range(6):  # alternate between two files it already read
        pr.note([calls[i % 2]])
        if pr.stalled():
            break
    assert pr.stalled()


def test_main_agent_stops_on_a_loop_with_a_notice(tmp_path):
    class Loop:
        def stream(self, model, messages, **kw):
            return TurnResult(tool_calls=[ToolCall("x", "read_file", json.dumps({"path": "a.txt"}))])

    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
    board = Board()
    agent = Agent(Loop(), {**BASE, "max_turns": 50}, ToolContext(root=tmp_path), Permissions(), board)
    assert agent.run("go") == ""
    assert agent.last_stats.model_calls == 3
    assert any("repeating the same read_file call" in n for n in board.notices)
