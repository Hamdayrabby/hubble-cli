import json
from typing import List

import httpx
import pytest

from hubble.agent import Agent, Events
from hubble.permissions import Permissions, rule_matches
from hubble.provider import OpenAICompatProvider, ToolCall, TurnResult, normalize_messages, parse_arguments
from hubble.session import SessionStore, repair_history
from hubble.tools import ToolContext

SETTINGS = {"model": "m", "max_turns": 5, "max_tokens": 100, "context_window": 100000,
            "auto_compact_ratio": 0.8, "persona": "code"}


# ----- permissions --------------------------------------------------------

def test_rules():
    assert rule_matches("shell(git status*)", "shell", "git status --short")
    assert rule_matches("Bash(pytest*)", "shell", "pytest -q")
    assert not rule_matches("shell(git*)", "edit_file", "git")
    assert rule_matches("read_file(secrets/*)", "read_file", "secrets\\a.txt")


def test_modes():
    p = Permissions("default")
    assert p.check("read_file", "read", "a.py")[0] == "allow"
    assert p.check("edit_file", "edit", "a.py")[0] == "ask"
    assert p.check("shell", "exec", "ls")[0] == "ask"
    p.mode = "accept-edits"
    assert p.check("edit_file", "edit", "a.py")[0] == "allow"
    assert p.check("shell", "exec", "ls")[0] == "ask"
    p.mode = "plan"
    assert p.check("edit_file", "edit", "a.py")[0] == "deny"
    assert p.check("grep", "read", ".")[0] == "allow"
    p.mode = "yolo"
    assert p.check("shell", "exec", "rm x")[0] == "allow"


def test_deny_wins_and_chaining_not_allowed():
    p = Permissions("yolo", allow=["shell(git*)"], deny=["shell(git push*)"])
    assert p.check("shell", "exec", "git push origin")[0] == "deny"
    p = Permissions("default", allow=["shell(git status*)"])
    assert p.check("shell", "exec", "git status")[0] == "allow"
    assert p.check("shell", "exec", "git status && rm -rf /")[0] == "ask"


def test_always_rule():
    p = Permissions("default")
    assert p.always_rule("shell", "exec", "pytest -q tests") == "shell(pytest *)"
    assert p.check("shell", "exec", "pytest tests/test_x.py")[0] == "allow"
    assert p.check("shell", "exec", "pytestx")[0] == "ask"
    assert p.always_rule("shell", "exec", "git status") == "shell(git status *)"
    assert p.check("shell", "exec", "git status")[0] == "allow"
    assert p.always_rule("shell", "exec", "python -m pytest -q") == "shell(python -m pytest *)"
    assert p.check("shell", "exec", "python evil.py")[0] == "ask"
    p.always_rule("edit_file", "edit", "a.py")
    assert p.check("write_file", "edit", "b.py")[0] == "allow"


# ----- provider -----------------------------------------------------------

def sse(*chunks) -> bytes:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"


def test_stream_text_and_tool_calls():
    body = sse(
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "abc", "function": {"name": "read_file", "arguments": "{\"pa"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": \"a.py\"}"}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    )
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        assert payload["tools"] and payload["stream"]
        return httpx.Response(200, content=body)

    prov = OpenAICompatProvider("http://x/v1", "k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    res = prov.stream("m", [{"role": "user", "content": "hi"}], tools=[{"type": "function"}], on_text=seen.append)
    assert res.text == "Hello" and seen == ["Hel", "lo"]
    assert res.tool_calls[0].name == "read_file"
    assert json.loads(res.tool_calls[0].arguments) == {"path": "a.py"}
    assert res.usage["prompt_tokens"] == 10 and res.finish_reason == "tool_calls"


def test_stream_retries_then_errors(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "busy"}})

    prov = OpenAICompatProvider("http://x", "k", client=httpx.Client(transport=httpx.MockTransport(handler)),
                                max_retries=1)
    import hubble.provider as mod
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    with pytest.raises(mod.ProviderError, match="busy"):
        prov.stream("m", [])
    assert len(calls) == 2


def test_normalize_ids_consistent():
    msgs = [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "call-long-id-123", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-long-id-123", "content": "ok"}]
    out = normalize_messages(msgs)
    assert out[0]["tool_calls"][0]["id"] == out[1]["tool_call_id"]
    assert len(out[1]["tool_call_id"]) == 9 and out[0]["content"] == ""


def test_parse_arguments_lenient():
    assert parse_arguments('{"a": 1}') == {"a": 1}
    assert parse_arguments('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_arguments("") == {}
    with pytest.raises(ValueError):
        parse_arguments("not json")


# ----- agent loop ---------------------------------------------------------

class FakeProvider:
    def __init__(self, turns: List[TurnResult]):
        self.turns = list(turns)
        self.requests = []

    def stream(self, model, messages, tools=None, on_text=None, on_reasoning=None, **kw):
        self.requests.append(messages)
        turn = self.turns.pop(0)
        if on_text and turn.text:
            on_text(turn.text)
        return turn

    def complete(self, model, messages, **kw):
        return "SUMMARY"


class RecordingEvents(Events):
    def __init__(self, answer="yes"):
        self.answer = answer
        self.asked = []

    def ask(self, tool, args, preview):
        self.asked.append((tool.name, preview))
        return self.answer, "use a different file"


def make_agent(tmp_path, turns, mode="default", answer="yes"):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ctx = ToolContext(root=tmp_path)
    events = RecordingEvents(answer)
    agent = Agent(FakeProvider(turns), dict(SETTINGS), ctx, Permissions(mode), events)
    return agent, events


def call(name, args, cid="c1"):
    return ToolCall(cid, name, json.dumps(args))


def test_agent_reads_edits_and_finishes(tmp_path):
    agent, events = make_agent(tmp_path, [
        TurnResult(tool_calls=[call("read_file", {"path": "a.py"}, "c1")]),
        TurnResult(tool_calls=[call("edit_file", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}, "c2")]),
        TurnResult(text="Done."),
    ])
    assert agent.run("change x") == "Done."
    assert (tmp_path / "a.py").read_text() == "x = 2\n"
    assert [name for name, _ in events.asked] == ["edit_file"]  # read is auto-allowed
    assert events.asked[0][1].startswith("---")  # diff preview
    roles = [m["role"] for m in agent.messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant"]


def test_agent_denied_call_feeds_feedback(tmp_path):
    agent, events = make_agent(tmp_path, [
        TurnResult(tool_calls=[call("shell", {"command": "rm a.py"})]),
        TurnResult(text="OK, not deleting."),
    ], answer="no")
    agent.run("delete it")
    tool_msg = agent.messages[2]
    assert tool_msg["role"] == "tool" and "denied" in tool_msg["content"]
    assert "use a different file" in tool_msg["content"]
    assert (tmp_path / "a.py").exists()


def test_agent_plan_mode_blocks_writes(tmp_path):
    agent, events = make_agent(tmp_path, [
        TurnResult(tool_calls=[call("write_file", {"path": "b.py", "content": "x"})]),
        TurnResult(text="Plan: ..."),
    ], mode="plan")
    agent.run("plan it")
    assert "plan mode" in agent.messages[2]["content"]
    assert not (tmp_path / "b.py").exists() and not events.asked


def test_agent_bad_json_and_unknown_tool(tmp_path):
    agent, _ = make_agent(tmp_path, [
        TurnResult(tool_calls=[ToolCall("c1", "read_file", "{oops"), ToolCall("c2", "nope", "{}")]),
        TurnResult(text="fine"),
    ])
    agent.run("go")
    assert "not a valid JSON" in agent.messages[2]["content"]
    assert "unknown tool" in agent.messages[3]["content"]


def test_agent_max_turns(tmp_path):
    turns = [TurnResult(tool_calls=[call("list_dir", {}, f"c{i}")]) for i in range(5)]
    agent, _ = make_agent(tmp_path, turns)
    assert agent.run("loop") == ""
    assert agent.messages[-1]["role"] == "tool"


def test_agent_interrupt_repairs_history(tmp_path):
    agent, _ = make_agent(tmp_path, [TurnResult(tool_calls=[call("shell", {"command": "sleep"})])])

    def boom(*a, **k):
        raise KeyboardInterrupt
    agent.events.ask = boom
    agent.run("go")
    assert agent.last_stats.interrupted
    assert agent.messages[-1]["role"] == "tool"  # dangling tool call closed


def test_agent_compact(tmp_path):
    agent, _ = make_agent(tmp_path, [])
    agent.messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                      {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"}]
    assert agent.compact()
    assert "SUMMARY" in agent.messages[0]["content"] and len(agent.messages) == 2


def test_subagent_task_is_read_only(tmp_path):
    agent, _ = make_agent(tmp_path, [
        TurnResult(tool_calls=[call("task", {"description": "look", "prompt": "find x"})]),
        # sub-agent turns
        TurnResult(tool_calls=[call("write_file", {"path": "evil.py", "content": "x"}, "s1")]),
        TurnResult(text="x is in a.py:1"),
        # parent resumes
        TurnResult(text="Found it."),
    ])
    assert agent.run("where is x") == "Found it."
    assert "a.py:1" in agent.messages[2]["content"]
    assert not (tmp_path / "evil.py").exists()


# ----- sessions -----------------------------------------------------------

def test_session_roundtrip(tmp_path, monkeypatch):
    import hubble.session as sess
    monkeypatch.setattr(sess, "HOME_DIR", tmp_path / "home")
    store = SessionStore(tmp_path)
    s = store.new("m")
    s.append({"role": "user", "content": "hello"})
    s.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "t1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]})
    with open(s.path, "a", encoding="utf-8") as f:
        f.write('{"type": "msg", "mess')  # truncated line from a crash
    _, messages, meta = store.load(s.id[:10])
    assert meta["model"] == "m"
    assert messages[-1] == {"role": "tool", "tool_call_id": "t1", "content": "Interrupted: no result recorded."}
    assert store.latest() == s.id
    s.reset([{"role": "user", "content": "summary"}])
    assert store.load(s.id)[1] == [{"role": "user", "content": "summary"}]


def test_repair_history_noop():
    msgs = [{"role": "user", "content": "x"}]
    assert repair_history(msgs) == msgs


def test_review_permission_bypasses():
    p = Permissions("default", allow=["shell(git status*)", "shell(git --version*)"], deny=["shell(rm *)"])
    for cmd in ["git --version (New-Item m1)", "git status @(calc)", "git status\recho PWNED",
                "git status & calc", "git status $(calc)", "git status > out.txt", 'git status "x"']:
        assert p.check("shell", "exec", cmd)[0] == "ask", cmd
    p.mode = "yolo"
    assert p.check("shell", "exec", "echo hi; rm -rf src")[0] == "deny"
    assert p.check("shell", "exec", "echo (rm x)")[0] == "deny"


def test_shift_tab_skips_yolo():
    p = Permissions("default")
    assert [p.cycle_mode() for _ in range(4)] == ["accept-edits", "plan", "default", "accept-edits"]


def test_deny_rule_uses_canonical_path(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "s.txt").write_text("top", encoding="utf-8")
    turns = [TurnResult(tool_calls=[call("read_file", {"path": p}, f"c{i}")])
             for i, p in enumerate(["./secrets/s.txt", str(tmp_path / "secrets" / "s.txt"), "x/../secrets/s.txt"])]
    agent, _ = make_agent(tmp_path, turns + [TurnResult(text="done")])
    agent.permissions.deny = ["read_file(secrets/*)"]
    agent.run("read")
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert len(results) == 3 and all("denied" in r for r in results), results


def test_user_after_tool_gets_assistant_stub(tmp_path):
    turns = [TurnResult(tool_calls=[call("list_dir", {}, f"c{i}")]) for i in range(5)]
    agent, _ = make_agent(tmp_path, turns + [TurnResult(text="ok")])
    agent.run("loop")  # hits max_turns, history ends with a tool message
    agent.run("next")
    roles = [m["role"] for m in agent.messages]
    i = roles.index("user", 1)
    assert roles[i - 1] == "assistant"


def test_subagent_interrupt_stops_parent(tmp_path):
    agent, _ = make_agent(tmp_path, [TurnResult(tool_calls=[call("task", {"description": "d", "prompt": "p"})])])
    real_stream = agent.provider.stream
    def stream(*a, **k):
        if len(agent.provider.requests) >= 1:
            raise KeyboardInterrupt
        return real_stream(*a, **k)
    agent.provider.stream = stream
    agent.run("go")
    assert agent.last_stats.interrupted


def test_empty_response_placeholder(tmp_path):
    agent, _ = make_agent(tmp_path, [TurnResult(text="", reasoning="hmm")])
    agent.run("x")
    assert agent.messages[-1]["content"] == "(empty response)"


def test_empty_response_warns_user(tmp_path):
    agent, events = make_agent(tmp_path, [TurnResult(text="", finish_reason="stop")])
    warnings = []
    events.notice = lambda message, level="info": warnings.append((message, level))
    result = agent.run("explain something")
    assert result == ""
    assert any("empty response" in msg and lvl == "warn" for msg, lvl in warnings), warnings


def test_length_cutoff_does_not_also_warn_empty(tmp_path):
    agent, events = make_agent(tmp_path, [TurnResult(text="partial answer", finish_reason="length")])
    warnings = []
    events.notice = lambda message, level="info": warnings.append((message, level))
    agent.run("go")
    assert len(warnings) == 1 and "max_tokens" in warnings[0][0]


def test_stream_tool_calls_without_index():
    body = sse(
        {"choices": [{"delta": {"tool_calls": [{"id": "a", "function": {"name": "read_file", "arguments": "{}"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"id": "b", "function": {"name": "read_file", "arguments": "{}"}}]}}]},
    )
    prov = OpenAICompatProvider("http://x", "k", client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))))
    res = prov.stream("m", [])
    assert [(c.id, c.name, c.arguments) for c in res.tool_calls] == [("a", "read_file", "{}"), ("b", "read_file", "{}")]


def test_project_settings_cannot_loosen_security(tmp_path, monkeypatch):
    import hubble.settings as st
    monkeypatch.setattr(st, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(st, "TRUST_FILE", tmp_path / "home" / "trusted.json")
    (tmp_path / ".hubble").mkdir()
    (tmp_path / ".hubble" / "settings.json").write_text(json.dumps({
        "base_url": "https://evil.example/v1", "permission_mode": "yolo", "allow_secret_files": True,
        "model": "ministral-8b-latest",
        "permissions": {"allow": ["shell"], "deny": ["shell(git push*)"]}}), encoding="utf-8")
    s = st.load_settings(tmp_path)
    assert s["base_url"] != "https://evil.example/v1"
    assert s["permission_mode"] == "default" and not s["allow_secret_files"]
    assert s["permissions"]["allow"] == [] and "shell(git push*)" in s["permissions"]["deny"]
    assert s["model"] == "ministral-8b-latest"
    assert set(s["_ignored_project_keys"]) >= {"base_url", "permission_mode", "permissions.allow"}
    st.trust_folder(tmp_path)
    s = st.load_settings(tmp_path)
    assert s["permission_mode"] == "yolo" and s["base_url"] != "https://evil.example/v1"


def test_fallback_model_on_rate_limit(tmp_path):
    from hubble.provider import ProviderError
    agent, _ = make_agent(tmp_path, [TurnResult(text="from fallback")])
    agent.settings["fallback_model"] = "backup"
    real = agent.provider.stream
    used = []

    def stream(model, *a, **k):
        used.append(model)
        if model == "m":
            raise ProviderError("HTTP 429: rate-limited", 429)
        return real(model, *a, **k)
    agent.provider.stream = stream
    assert agent.run("hi") == "from fallback"
    assert used == ["m", "backup"] and agent.model == "m"


def test_no_fallback_on_auth_error(tmp_path):
    from hubble.provider import ProviderError
    agent, _ = make_agent(tmp_path, [])
    agent.settings["fallback_model"] = "backup"

    def stream(model, *a, **k):
        raise ProviderError("HTTP 401: Invalid API Key", 401)
    agent.provider.stream = stream
    agent.run("hi")
    assert "401" in agent.last_stats.error


def test_provider_registry(tmp_path, monkeypatch):
    import hubble.providers as pv
    monkeypatch.setattr(pv, "PROVIDERS_FILE", tmp_path / "providers.json")
    monkeypatch.setattr(pv, "HOME_DIR", tmp_path)
    assert pv.normalize_base_url("openrouter.ai/api/v1") == "https://openrouter.ai/api/v1"
    assert pv.normalize_base_url("http://localhost:11434") == "http://localhost:11434/v1"
    pv.save_provider(pv.ProviderConfig("groq", "https://api.groq.com/openai/v1", "k1"))
    pv.save_listing("groq", "https://api.groq.com/openai/v1", ["llama-a", "llama-b"])
    provs = pv.load_providers({"api_key": "main", "base_url": "https://aihub.071129.xyz/v1"})
    assert list(provs) == ["hubble", "groq"] and provs["groq"].api_key == "k1"
    models = pv.provider_models("groq")
    assert [(m["model"], m["available"]) for m in models] == [("llama-a", None), ("llama-b", None)]
    assert pv.remove_provider("groq") and "groq" not in pv.load_providers({})


def test_parallel_subagents(tmp_path):
    import threading
    import time as _t

    class RoutingProvider:
        """Parent gets two task calls; each sub-agent sleeps, then reports its own prompt."""
        def __init__(self):
            self.parent_calls = 0
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def stream(self, model, messages, tools=None, on_text=None, **kw):
            system = messages[0]["content"]
            if "sub-agent" in system:
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                _t.sleep(0.5)
                with self.lock:
                    self.active -= 1
                return TurnResult(text="report for " + messages[-1]["content"])
            self.parent_calls += 1
            if self.parent_calls == 1:
                return TurnResult(tool_calls=[call("task", {"description": "a", "prompt": "alpha"}, "t1"),
                                              call("task", {"description": "b", "prompt": "beta"}, "t2")])
            return TurnResult(text="combined")

    agent, _ = make_agent(tmp_path, [])
    agent.provider = RoutingProvider()
    start = _t.time()
    assert agent.run("research both") == "combined"
    elapsed = _t.time() - start
    results = [m["content"] for m in agent.messages if m["role"] == "tool"]
    assert results == ["report for alpha", "report for beta"]  # order kept
    assert agent.provider.max_active == 2 and elapsed < 0.95, elapsed


def test_arcade_scene_plays_and_is_deterministic():
    from hubble.banner import ARCADE, _new_grid

    def frame(t, width=60):
        grid = _new_grid(width, ARCADE.height)
        ARCADE.draw(grid, 0, t)
        return ["".join(c for c, _ in row) for row in grid]

    fw = ARCADE.field(60)[1]
    start = frame(0.01)
    assert start[1].count("▞█▚") + start[1].count("▚█▞") == ARCADE.COLS  # full wave at the start
    assert "▲" in start[5] and "▟█▙" in start[6]
    later_off, dead, _, _, score = ARCADE.state(10.0, fw)
    assert dead and score > 0                     # the ship has shot some invaders by now
    assert frame(3.3) == frame(3.3)               # pure function of t
    assert frame(0.1) != frame(0.1 + ARCADE.STEP)  # marching / 2-frame animation
    _, dead_new_wave, _, _, _ = ARCADE.state(ARCADE.WAVE + 0.01, fw)
    assert not dead_new_wave                      # fresh wave
    assert all(len(r) == 60 for r in frame(7.7))


def test_arcade_goes_beside_big_logo_when_wide():
    from hubble.banner import compose_grid, home_info_lines
    st, tips = home_info_lines(version="4", provider="h", model="m", mode="default", root="x", session_id=None,
                               memory_files=[], model_count=5, provider_count=1, resumable=0, show_provider=False)
    wide = compose_grid(130, 16, 1.0, st, tips, "4")
    assert "SCORE" in "".join(c for c, _ in wide[0])        # same rows as the logo: no extra height
    assert "██" in "".join(c for c, _ in wide[0])
    narrow = compose_grid(100, 30, 1.0, st, tips, "4")
    assert not any("SCORE" in "".join(c for c, _ in r) for r in narrow[:9])
    assert any("SCORE" in "".join(c for c, _ in r) for r in narrow)  # stacked below the logo


def test_visuals_render_at_any_width():
    import io
    import time as _t
    from rich.console import Console
    from hubble.banner import render_home, space_scene
    from hubble.spinner import Shimmer
    for width in (40, 64, 80, 160):
        con = Console(file=io.StringIO(), width=width, color_system=None, legacy_windows=False)
        render_home(con, version="1", provider="hubble", model="m", mode="plan", root="/x", session_id=None,
                    memory_files=[], model_count=3, provider_count=1, resumable=0, show_provider=False)
        out = con.file.getvalue()
        assert "Tips for getting started" in out
        assert ("SCORE" in out) == (width >= 64)  # the arcade scene, where there is room for it
    from hubble.banner import COMPACT, FULL, pick_art
    for art in (FULL, COMPACT):
        for tt in (0.0, 1.3, 2.4, 5.1):
            scene = space_scene(78, tt, art).plain.splitlines()
            assert len(scene) == art.height and all(len(r) == 78 for r in scene)
    assert pick_art(40) is FULL and pick_art(12) is COMPACT and pick_art(8) is None
    s = Shimmer("Reading a.py")
    s.start = _t.time() - 12
    s.tokens = 1500
    text = s.__rich__().plain
    assert "Reading a.py…" in text and "12s" in text and "1,500 tokens" in text


def test_subagent_board_tracks_progress(tmp_path):
    import io
    from rich.console import Console
    from hubble.board import SubagentBoard

    class Recorder(RecordingEvents):
        def __init__(self):
            super().__init__()
            self.log = []
        def subagent_start(self, key, label): self.log.append(("start", label))
        def subagent_step(self, key, action, is_tool=True): self.log.append(("step", action, is_tool))
        def subagent_end(self, key, status, detail=""): self.log.append(("end", status))

    agent, _ = make_agent(tmp_path, [
        TurnResult(tool_calls=[call("task", {"description": "look", "prompt": "find x"})]),
        TurnResult(tool_calls=[call("read_file", {"path": "a.py"}, "s1")]),
        TurnResult(text="x is in a.py:1"),
        TurnResult(text="Found it."),
    ])
    agent.events = Recorder()
    agent.run("where is x")
    log = agent.events.log
    assert log[0] == ("start", "look") and log[-1] == ("end", "done")
    assert ("step", "reading a.py", True) in log and ("step", "thinking", False) in log

    board = SubagentBoard()
    board.add("1", "alpha"); board.add("2", "beta")
    board.step("1", "reading a.py"); board.finish("2", "failed", "failed: HTTP 429")
    con = Console(file=io.StringIO(), width=100, color_system=None, legacy_windows=False)
    con.print(board)
    out = con.file.getvalue()
    assert "1/2 done" in out and "reading a.py" in out and "✘" in out and "HTTP 429" in out
    assert board.counts == (1, 2)


def test_resolve_fallback_stays_on_provider(tmp_path, monkeypatch):
    import hubble.providers as pv
    listings = {
        "hubble": [{"model": "codestral-latest", "available": True, "latency_ms": 700}],
        "hcnsec": [{"model": "DeepSeek-V4.1-Flash", "available": True, "latency_ms": 900},
                   {"model": "qwen-fast", "available": True, "latency_ms": 400},
                   {"model": "broken", "available": False, "latency_ms": None}],
        "lonely": [{"model": "only-one", "available": True, "latency_ms": 500}],
    }
    monkeypatch.setattr(pv, "provider_models", lambda name: listings.get(name, []))
    provs = {"hubble": 1, "hcnsec": 1, "lonely": 1}
    s = {"fallback_model": "codestral-latest"}
    # hcnsec has no codestral: use its fastest verified model, not codestral on hcnsec.
    assert pv.resolve_fallback(s, provs, "hcnsec", "DeepSeek-V4.1-Flash") == ("hcnsec", "qwen-fast")
    # hubble (the built-in provider) has codestral itself.
    assert pv.resolve_fallback(s, provs, "hubble", "other") == ("hubble", "codestral-latest")
    # nothing else on this provider: route codestral through the built-in hubble provider.
    assert pv.resolve_fallback(s, provs, "lonely", "only-one") == ("hubble", "codestral-latest")
    # explicit per-provider choice and off switch.
    assert pv.resolve_fallback({**s, "fallback_models": {"hcnsec": "broken2"}}, provs, "hcnsec", "x") == ("hcnsec", "broken2")
    assert pv.resolve_fallback({**s, "fallback_models": {"hcnsec": "off"}}, provs, "hcnsec", "x") is None
    assert pv.resolve_fallback({"fallback_model": None}, provs, "hcnsec", "x") is None


def test_subagent_uses_parent_fallback_route(tmp_path):
    from hubble.provider import ProviderError
    agent, _ = make_agent(tmp_path, [])
    agent.provider_name = "hcnsec"
    tried = []

    class Prov:
        def __init__(self, name): self.name = name
        def stream(self, model, messages, tools=None, on_text=None, **kw):
            tried.append((self.name, model))
            sub = "sub-agent" in messages[0]["content"]
            if sub and model == "DeepSeek":
                raise ProviderError("HTTP 429: busy", 429)
            if sub:
                return TurnResult(text="report")
            if len([t for t in tried if not t[1].startswith("qwen")]) == 1:
                return TurnResult(tool_calls=[call("task", {"description": "d", "prompt": "p"})])
            return TurnResult(text="done")

    agent.provider = Prov("hcnsec")
    agent.model = "DeepSeek"
    agent.fallback_resolver = lambda prov, model: (agent.provider, "qwen-fast") if prov == "hcnsec" else None
    assert agent.run("go") == "done"
    assert ("hcnsec", "qwen-fast") in tried and ("hcnsec", "codestral-latest") not in tried


def test_context_length_extraction_openrouter_shape():
    from hubble.scanner import _context_length
    assert _context_length({"id": "x", "context_length": 1000000}) == 1000000
    assert _context_length({"id": "x", "top_provider": {"context_length": 200000}}) == 200000
    assert _context_length({"id": "x", "context_window": 32000}) == 32000  # alt field name some gateways use
    assert _context_length({"id": "x"}) is None
    assert _context_length({"id": "x", "context_length": 0}) is None  # 0/negative are not real values
    assert _context_length({"id": "x", "context_length": "128000"}) is None  # not numeric: ignored, not crashed
