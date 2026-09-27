import json
from types import SimpleNamespace

from hubble.main import run_headless
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.session import SessionStore
from hubble.tools import ToolContext

SETTINGS = {"model": "m", "max_turns": 6, "max_tokens": 100, "context_window": 100000,
            "auto_compact_ratio": 0, "persona": "code", "web_tools": False}


class FakeProvider:
    def __init__(self, turns):
        self.turns = list(turns)

    def stream(self, model, messages, on_text=None, **kw):
        turn = self.turns.pop(0)
        if on_text and turn.text:
            on_text(turn.text)
        return turn


def test_stream_json_emits_one_event_per_line(tmp_path, capsys):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    provider = FakeProvider([
        TurnResult(tool_calls=[ToolCall("c1", "read_file", json.dumps({"path": "a.txt"}))]),
        TurnResult(text="It says hello."),
    ])
    args = SimpleNamespace(output_format="stream-json", quiet=False, continue_=False, resume=None,
                           no_session=True, _providers={})
    code = run_headless(args, dict(SETTINGS), provider, ToolContext(root=tmp_path), Permissions(),
                        SessionStore(tmp_path), "what does a.txt say")
    assert code == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    types = [e["type"] for e in events]
    assert types[0] == "init" and types[-1] == "result"
    assert "tool_use" in types and "tool_result" in types and "text" in types
    assert events[-1]["result"] == "It says hello." and events[-1]["num_tool_calls"] == 1
