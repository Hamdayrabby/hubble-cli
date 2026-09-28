import _thread
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hubble.agent import Agent, Events, pick_team_model
from hubble.permissions import Permissions
from hubble.provider import OpenAICompatProvider, ProviderError, ToolCall, TurnResult
from hubble.tools import Shell, ToolContext, detect_shell

BASE = {"model": "m", "max_turns": 4, "max_tokens": 10, "context_window": 1000, "auto_compact_ratio": 0,
        "persona": "code", "web_tools": False}


def press_ctrl_c_after(seconds):
    t = threading.Timer(seconds, _thread.interrupt_main)
    t.start()
    return t


class _Stall(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    status = 200

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'data: {"choices": [{"delta": {"content": "thinking..."}}]}\n\n')
        self.wfile.flush()
        time.sleep(15)  # a model that goes quiet mid-answer


@pytest.fixture
def stalling_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stall)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()
    srv.server_close()


def test_ctrl_c_stops_a_stalled_stream_promptly(stalling_server, tmp_path):
    agent = Agent(OpenAICompatProvider(stalling_server, "k"), dict(BASE), ToolContext(root=tmp_path),
                  Permissions(), Events())
    timer = press_ctrl_c_after(1.0)
    t0 = time.time()
    agent.run("hello")
    timer.cancel()
    assert agent.last_stats.interrupted
    assert time.time() - t0 < 4, "Ctrl+C waited for the stalled model instead of stopping"


def test_ctrl_c_stops_a_long_shell_command(tmp_path):
    ctx = ToolContext(root=tmp_path, shell_argv=detect_shell())
    cmd = f'"{sys.executable}" -c "import time; time.sleep(20)"'
    if "powershell" in ctx.shell_argv[0].lower() or "pwsh" in ctx.shell_argv[0].lower():
        cmd = "& " + cmd
    timer = press_ctrl_c_after(1.0)
    t0 = time.time()
    with pytest.raises(KeyboardInterrupt):
        Shell().run({"command": cmd, "timeout": 60}, ctx)
    timer.cancel()
    assert time.time() - t0 < 5


def test_shell_timeout_still_enforced_in_slices(tmp_path):
    from hubble.tools import ToolError
    ctx = ToolContext(root=tmp_path, shell_argv=detect_shell())
    cmd = f'"{sys.executable}" -c "import time; time.sleep(10)"'
    if "powershell" in ctx.shell_argv[0].lower() or "pwsh" in ctx.shell_argv[0].lower():
        cmd = "& " + cmd
    t0 = time.time()
    with pytest.raises(ToolError, match="timed out after 1s"):
        Shell().run({"command": cmd, "timeout": 1}, ctx)
    assert time.time() - t0 < 6


class _Once401(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).hits += 1
        if type(self).hits == 1:
            body = b'{"error": {"message": "Invalid API Key"}}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        payload = b'data: {"choices": [{"delta": {"content": "hi"}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n'
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def test_spurious_401_is_retried_once():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Once401)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = OpenAICompatProvider(f"http://127.0.0.1:{srv.server_address[1]}/v1", "k").stream(
            "m", [{"role": "user", "content": "x"}])
        assert r.text == "hi" and _Once401.hits == 2
    finally:
        srv.shutdown()
        srv.server_close()


# ----- sub-agent retry on another team model -----------------------------------

class Prov:
    def __init__(self, name, log, parent=None, fail_models=()):
        self.hubble_name, self.log, self.parent, self.fail = name, log, list(parent or []), set(fail_models)

    def stream(self, model, messages, **kw):
        sub = "sub-agent" in messages[0]["content"]
        self.log.append((self.hubble_name, model, "sub" if sub else "main"))
        if sub:
            if model in self.fail:
                raise ProviderError("HTTP 404: model not found", 404)
            return TurnResult(text=f"report from {model}")
        return self.parent.pop(0)


TEAM = {"subagent_models": [{"model": "aihub:flaky-fast", "use": "fast, for searching"},
                            {"model": "groq:steady", "use": "careful reviewer"}]}


def test_failed_subagent_retries_on_next_team_model(tmp_path):
    log = []
    aihub = Prov("aihub", log, parent=[
        TurnResult(tool_calls=[ToolCall("t1", "task", json.dumps({"description": "look", "prompt": "find"}))]),
        TurnResult(text="done")], fail_models={"flaky-fast"})
    provs = {"aihub": aihub, "groq": Prov("groq", log)}
    agent = Agent(aihub, {**BASE, "provider": "aihub", **TEAM}, ToolContext(root=tmp_path), Permissions(), Events())
    agent.client_for = provs.get
    agent.run("research something")
    subs = [(p, m) for p, m, kind in log if kind == "sub"]
    assert subs == [("aihub", "flaky-fast"), ("groq", "steady")]   # auto-picked fast one, then retried
    report = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert "report from steady" in report and "after the first model failed" in report


def test_edit_subagent_is_not_retried_after_it_acted(tmp_path):
    log = []

    class ActsThenFails(Prov):
        def stream(self, model, messages, **kw):
            sub = "sub-agent" in messages[0]["content"]
            self.log.append((self.hubble_name, model, "sub" if sub else "main"))
            if not sub:
                return self.parent.pop(0)
            if any(m.get("role") == "tool" for m in messages):
                raise ProviderError("HTTP 404: gone", 404)
            return TurnResult(tool_calls=[ToolCall("w", "write_file", json.dumps({"path": "a.txt", "content": "x"}))])

    aihub = ActsThenFails("aihub", log, parent=[
        TurnResult(tool_calls=[ToolCall("t1", "task", json.dumps({"description": "change", "prompt": "edit",
                                                                    "capability": "edit"}))]),
        TurnResult(text="done")])
    team = {"subagent_models": [{"model": "groq:steady", "use": "fast searcher"}]}  # nothing edit-suited
    agent = Agent(aihub, {**BASE, "provider": "aihub", **team}, ToolContext(root=tmp_path),
                  Permissions("accept-edits"), Events())
    agent.client_for = {"aihub": aihub, "groq": Prov("groq", log)}.get
    agent.run("make a change")
    assert ("aihub", "m", "sub") in log                          # the edit ran on the main model
    assert not any(p == "groq" for p, m, k in log)               # never re-run a half-done edit elsewhere
    tool_out = next(m["content"] for m in agent.messages if m.get("role") == "tool")
    assert "after making changes" in tool_out


def test_pick_team_model_by_job():
    assert pick_team_model(TEAM, edit=False) == "aihub:flaky-fast"
    assert pick_team_model(TEAM, edit=True) == "groq:steady"
    plain = {"subagent_models": [{"model": "aihub:a", "use": ""}]}
    assert pick_team_model(plain, edit=False) == "aihub:a"
    assert pick_team_model(plain, edit=True) == ""               # edits stay on the main model
    assert pick_team_model({}, edit=False) == ""


# ----- prompt: Ctrl+C clears text, twice on empty exits -----------------------

def test_ctrl_c_twice_on_empty_prompt_exits(tmp_path):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from hubble.repl import Repl
    from hubble.session import SessionStore
    from hubble.ui import ReplEvents

    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, dict(BASE), ctx, Permissions(), ReplEvents(ctx))
    repl = Repl(agent, SessionStore(tmp_path), {"aihub": object()})
    repl._refresh_stale_scans = lambda: None
    repl.banner = lambda: None
    sent = []
    repl.handle = lambda text: sent.append(text)
    with create_pipe_input() as inp:
        def typer():
            time.sleep(0.3)
            inp.send_text("half typed")
            time.sleep(0.2)
            inp.send_text("\x03")          # Ctrl+C with text: clears it, does not exit
            time.sleep(0.2)
            inp.send_text("real\r")
            time.sleep(0.3)
            inp.send_text("\x03")          # empty prompt: "press again to exit"
            time.sleep(0.2)
            inp.send_text("\x03")          # second one: exit
        threading.Thread(target=typer, daemon=True).start()
        with create_app_session(input=inp, output=DummyOutput()):
            t0 = time.time()
            repl.run()
    assert sent == ["real"]                 # the cleared text was never sent
    assert time.time() - t0 < 5
