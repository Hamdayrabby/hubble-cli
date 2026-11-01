import base64

from hubble.agent import Agent, Events, estimate_messages
from hubble.images import IMAGE_TOKEN_ESTIMATE, content_text, data_url, user_content
from hubble.permissions import Permissions
from hubble.provider import TurnResult
from hubble.session import SessionStore
from hubble.tools import ToolContext

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
SETTINGS = {"model": "m", "max_turns": 3, "max_tokens": 100, "context_window": 100000,
            "auto_compact_ratio": 0, "persona": "code", "web_tools": False}


class CaptureProvider:
    def stream(self, model, messages, **kw):
        self.messages = messages
        return TurnResult(text="a red dot")


def test_data_url_and_user_content(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(PNG)
    url = data_url(img)
    assert url.startswith("data:image/png;base64,")
    content = user_content("what is this?", [url])
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["image_url"]["url"] == url
    assert content_text(content) == "what is this?\n[image]"
    assert user_content("plain", None) == "plain"


def test_agent_sends_image_parts_and_estimates_them(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(PNG)
    provider = CaptureProvider()
    agent = Agent(provider, dict(SETTINGS), ToolContext(root=tmp_path), Permissions(), Events())
    assert agent.run("describe", [data_url(img)]) == "a red dot"
    user = provider.messages[-1]
    assert isinstance(user["content"], list) and user["content"][1]["type"] == "image_url"
    assert estimate_messages([user]) >= IMAGE_TOKEN_ESTIMATE


def test_repl_mention_attaches_image(tmp_path):
    from hubble.repl import Repl
    from hubble.ui import ReplEvents
    (tmp_path / "shot.png").write_bytes(PNG)
    ctx = ToolContext(root=tmp_path)
    provider = CaptureProvider()
    agent = Agent(provider, dict(SETTINGS), ctx, Permissions(), ReplEvents(ctx))
    repl = Repl(agent, SessionStore(tmp_path), {"hubble": object()})
    repl.send("what is in @shot.png")
    parts = provider.messages[-1]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url"]
    assert "<file" not in parts[0]["text"]  # not also dumped as text


def test_pasted_image_placeholder_controls_attachment(tmp_path):
    from hubble.repl import Repl
    from hubble.ui import ReplEvents
    ctx = ToolContext(root=tmp_path)
    provider = CaptureProvider()
    agent = Agent(provider, dict(SETTINGS), ctx, Permissions(), ReplEvents(ctx))
    repl = Repl(agent, SessionStore(tmp_path), {"hubble": object()})
    kept, dropped = tmp_path / "a.png", tmp_path / "b.png"
    kept.write_bytes(PNG)
    dropped.write_bytes(PNG)
    repl._pending_images = [kept, dropped]
    repl.send("look at [Image #1]")  # #2's placeholder was deleted before sending
    parts = provider.messages[-1]["content"]
    assert sum(1 for p in parts if p["type"] == "image_url") == 1
    assert repl._pending_images == [] and not kept.exists()  # temp files cleaned up


def test_session_title_from_image_message(tmp_path):
    store = SessionStore(tmp_path)
    s = store.new("m")
    s.append({"role": "user", "content": user_content("fix this layout", ["data:image/png;base64,AA=="])})
    assert store.list()[0]["title"].startswith("fix this layout")


def _repl(tmp_path):
    from hubble.repl import Repl
    from hubble.ui import ReplEvents

    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, dict(SETTINGS), ctx, Permissions(), ReplEvents(ctx))
    return Repl(agent, SessionStore(tmp_path), {"hubble": object()})


def test_ctrl_v_pastes_clipboard_image(tmp_path, monkeypatch):
    """Ctrl+V inserts the placeholder wherever the terminal forwards the key to us."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    import hubble.repl as repl_mod

    repl = _repl(tmp_path)
    img = tmp_path / "clip.png"
    img.write_bytes(PNG)
    monkeypatch.setattr(repl_mod, "grab_clipboard_image", lambda: img)
    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()):
            session = repl._prompt_session()
            inp.send_text("\x16")   # Ctrl+V
            inp.send_text("\r")
            text = session.prompt("> ")
    assert text.strip() == "[Image #1]"
    assert repl._pending_images == [img]


def test_pending_images_show_in_prompt_panel(tmp_path):
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.formatted_text.utils import fragment_list_to_text

    repl = _repl(tmp_path)
    img = tmp_path / "clip.png"
    img.write_bytes(PNG)
    repl._pending_images = [img]
    assert "[1 image]" in fragment_list_to_text(to_formatted_text(repl._toolbar()))
    repl.is_busy = True
    assert "[1 image]" in fragment_list_to_text(to_formatted_text(repl._toolbar()))


def test_queued_prompt_keeps_its_own_pasted_image(tmp_path):
    """A prompt queued while busy owns the image pasted with it; a later paste cannot be
    consumed by the earlier message (and the temp file is cleaned up after sending)."""
    from types import SimpleNamespace

    repl = _repl(tmp_path)
    img = tmp_path / "clip.png"
    img.write_bytes(PNG)
    repl._worker_thread = SimpleNamespace(is_alive=lambda: True)
    repl.is_busy = True
    repl._pending_images = [img]
    repl.handle("look at [Image #1]")
    queued = repl.message_queue[-1]
    assert queued.images == [img] and repl._pending_images == []
    seen = {}

    def fake_run(prompt, images=None):
        seen["content"] = images
        return "ok"

    repl.agent.run = fake_run
    repl.send(queued)
    assert len(seen["content"]) == 1 and seen["content"][0].startswith("data:image/png;base64,")
    assert not img.exists()
