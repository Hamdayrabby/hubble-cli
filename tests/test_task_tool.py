import json

from hubble.agent import Agent, Events
from hubble.permissions import Permissions
from hubble.provider import ToolCall, TurnResult
from hubble.tools import ToolContext

SETTINGS = {"model": "m", "max_turns": 6, "max_tokens": 100, "context_window": 100000,
            "auto_compact_ratio": 0, "persona": "code"}


class RecordingProvider:
    """Pops (text|tool_calls) turns in order, regardless of which model/messages asked for them;
    records which model each call used so per-task model overrides can be checked."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.models_used = []

    def stream(self, model, messages, tools=None, on_text=None, on_reasoning=None, **kw):
        self.models_used.append(model)
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
        return self.answer, "not now"


def make_agent(tmp_path, turns, mode="default", answer="yes"):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ctx = ToolContext(root=tmp_path)
    events = RecordingEvents(answer)
    provider = RecordingProvider(turns)
    agent = Agent(provider, dict(SETTINGS), ctx, Permissions(mode), events)
    return agent, events, provider


def call(name, args, cid="c1"):
    return ToolCall(cid, name, json.dumps(args))


def test_edit_capable_subagent_writes_a_file_with_approval(tmp_path):
    agent, events, _ = make_agent(tmp_path, [
        TurnResult(tool_calls=[call("task", {"description": "impl", "prompt": "add a new file",
                                            "capability": "edit"})]),
        # sub-agent's own turns:
        TurnResult(tool_calls=[call("write_file", {"path": "new.txt", "content": "hello"}, "s1")]),
        TurnResult(text="Created new.txt with the requested content."),
        # parent resumes:
        TurnResult(text="Done, delegated it."),
    ])
    result = agent.run("add a file via a sub-agent")
    assert result == "Done, delegated it."
    assert (tmp_path / "new.txt").read_text() == "hello"
    assert any(n == "write_file" for n, _ in events.asked)  # the edit really did ask via the parent's UI


def test_edit_capable_subagent_denied_edit_is_not_applied(tmp_path):
    agent, events, _ = make_agent(tmp_path, [
        TurnResult(tool_calls=[call("task", {"description": "impl", "prompt": "add a file",
                                            "capability": "edit"})]),
        TurnResult(tool_calls=[call("write_file", {"path": "new.txt", "content": "hello"}, "s1")]),
        TurnResult(text="Could not make the change; user declined."),
        TurnResult(text="OK, did not make the change."),
    ], answer="no")
    agent.run("add a file")
    assert not (tmp_path / "new.txt").exists()
    assert any(n == "write_file" for n, _ in events.asked)


def test_read_only_subagent_cannot_write(tmp_path):
    agent, events, _ = make_agent(tmp_path, [
        TurnResult(tool_calls=[call("task", {"description": "research", "prompt": "look around"})]),
        # capability defaults to read_only: write_file is not in its toolset at all
        TurnResult(tool_calls=[call("write_file", {"path": "new.txt", "content": "hello"}, "s1")]),
        TurnResult(text="I could not write; no such tool. Reporting findings only."),
        TurnResult(text="Done."),
    ])
    agent.run("research something")
    assert not (tmp_path / "new.txt").exists()
    assert events.asked == []  # never even reached an approval prompt; the tool doesn't exist for it


def test_edit_subagent_respects_parent_plan_mode(tmp_path):
    agent, events, _ = make_agent(tmp_path, [
        TurnResult(tool_calls=[call("task", {"description": "impl", "prompt": "add a file",
                                            "capability": "edit"})]),
        TurnResult(tool_calls=[call("write_file", {"path": "new.txt", "content": "hello"}, "s1")]),
        TurnResult(text="Blocked: plan mode."),
        TurnResult(text="OK."),
    ], mode="plan")
    agent.run("add a file")
    assert not (tmp_path / "new.txt").exists()
    assert events.asked == []  # plan mode denies outright, never gets to asking


def test_per_task_model_override(tmp_path):
    agent, events, provider = make_agent(tmp_path, [
        TurnResult(tool_calls=[call("task", {"description": "cheap lookup", "prompt": "find X",
                                            "model": "fast-model"})]),
        TurnResult(text="found it"),
        TurnResult(text="reported"),
    ])
    agent.run("find X using a cheaper model")
    assert provider.models_used[0] == "m"           # parent's own model
    assert provider.models_used[1] == "fast-model"  # sub-agent used the override
    assert provider.models_used[2] == "m"           # parent resumes on its own model


def test_task_tool_schema_offers_capability_and_model():
    from hubble.agent import TaskTool
    tool = TaskTool(parent=None)
    props = tool.parameters["properties"]
    assert set(props["capability"]["enum"]) == {"read_only", "edit"}
    assert "model" in props


def test_parallel_ok_excludes_edit_capable_batches(tmp_path):
    agent, _, _ = make_agent(tmp_path, [])
    read_only = call("task", {"description": "a", "prompt": "p"}, "c1")
    edit = call("task", {"description": "b", "prompt": "p", "capability": "edit"}, "c2")
    assert agent._parallel_ok([read_only, read_only.__class__("c3", "task", read_only.arguments)])
    assert not agent._parallel_ok([read_only, edit])


def test_apply_known_context_window_on_model_switch(tmp_path, monkeypatch):
    import hubble.repl as repl_mod
    from hubble.permissions import Permissions
    from hubble.repl import Repl
    from hubble.session import SessionStore
    from hubble.ui import ReplEvents

    monkeypatch.setattr(repl_mod, "provider_models",
                        lambda name: [{"model": "big-context-model", "context_length": 1000000},
                                      {"model": "small-model", "context_length": 8000}])

    def fake_save(key, value):
        pass
    monkeypatch.setattr(repl_mod, "save_user_setting", fake_save)

    ctx = ToolContext(root=tmp_path)
    settings = {**SETTINGS, "context_window": 128000}
    agent = Agent(RecordingProvider([]), settings, ctx, Permissions(), ReplEvents(ctx))
    agent.provider_name = "hubble"
    repl = Repl(agent, SessionStore(tmp_path), {"hubble": object()})

    repl._set_model("big-context-model")
    assert agent.settings["context_window"] == 1000000

    repl._set_model("small-model")
    assert agent.settings["context_window"] == 8000

    # A model the provider says nothing about keeps whatever was last known, rather than resetting.
    monkeypatch.setattr(repl_mod, "provider_models", lambda name: [{"model": "unlisted-model"}])
    repl._set_model("unlisted-model")
    assert agent.settings["context_window"] == 8000


def test_context_window_auto_can_be_disabled(tmp_path, monkeypatch):
    import hubble.repl as repl_mod
    from hubble.permissions import Permissions
    from hubble.repl import Repl
    from hubble.session import SessionStore
    from hubble.ui import ReplEvents

    monkeypatch.setattr(repl_mod, "provider_models",
                        lambda name: [{"model": "big-context-model", "context_length": 1000000}])
    monkeypatch.setattr(repl_mod, "save_user_setting", lambda key, value: None)

    ctx = ToolContext(root=tmp_path)
    settings = {**SETTINGS, "context_window": 128000, "context_window_auto": False}
    agent = Agent(RecordingProvider([]), settings, ctx, Permissions(), ReplEvents(ctx))
    agent.provider_name = "hubble"
    repl = Repl(agent, SessionStore(tmp_path), {"hubble": object()})

    repl._set_model("big-context-model")
    assert agent.settings["context_window"] == 128000  # untouched: auto-detection was turned off


def test_menu_space_survives_the_home_screen_prompt(tmp_path, monkeypatch):
    """Regression test: PromptSession.prompt(**kwargs) permanently overwrites the session, not
    just that call. The animated home screen's first prompt passes reserve_space_for_menu=0;
    without resetting it afterward, every later prompt loses room for the "/" completion menu."""
    import threading
    import time

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from hubble.agent import Agent
    from hubble.banner import home_info_lines
    from hubble.permissions import Permissions
    from hubble.repl import Repl
    from hubble.session import SessionStore
    from hubble.ui import ReplEvents

    ctx = ToolContext(root=tmp_path)
    agent = Agent(RecordingProvider([]), dict(SETTINGS), ctx, Permissions(), ReplEvents(ctx))
    repl = Repl(agent, SessionStore(tmp_path))
    repl._home = home_info_lines(version="4", provider="hubble", model="m", mode="default", root=str(tmp_path),
                                 session_id=None, memory_files=[], model_count=None, provider_count=1,
                                 resumable=0, show_provider=False)
    repl._home_t0 = time.time()

    with create_pipe_input() as inp:
        def typer():
            time.sleep(0.2)
            inp.send_text("\r")            # first prompt: just press Enter (the home screen)
            time.sleep(0.2)
            inp.send_text("/exit\r")        # second prompt: exit cleanly once we've checked state
        threading.Thread(target=typer, daemon=True).start()
        with create_app_session(input=inp, output=DummyOutput()):
            session = repl._prompt_session()
            assert session.reserve_space_for_menu == 14  # the constructed default

            first = session.prompt(repl._home_message, refresh_interval=1 / 30, reserve_space_for_menu=0)
            assert first == ""
            assert session.reserve_space_for_menu == 0  # confirms the underlying prompt_toolkit behavior

            # This is the fix under test: repl.run()'s loop resets these right after the home prompt.
            session.reserve_space_for_menu = 14
            session.refresh_interval = 1.0
            assert session.reserve_space_for_menu == 14

            second = session.prompt(HTML("> "))
    assert second == "/exit"
