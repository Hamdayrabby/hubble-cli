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
