import json

import pytest

from fake_anthropic import FakeAnthropic, text_reply, tool_reply  # tests/ is on sys.path under pytest
from hubble.agent import Agent, Events
from hubble.anthropic_provider import AnthropicProvider, anthropic_base_url, to_anthropic, to_anthropic_tools
from hubble.permissions import Permissions
from hubble.provider import ProviderError, normalize_messages
from hubble.tools import ToolContext

SETTINGS = {"model": "claude-opus-5", "max_turns": 6, "max_tokens": 1000, "context_window": 100000,
            "auto_compact_ratio": 0, "persona": "code", "web_tools": False}


@pytest.fixture
def fake(request):
    s = FakeAnthropic(getattr(request, "param", []))
    yield s
    s.close()


def test_history_conversion_blocks_tools_images_and_merged_results():
    hist = [
        {"role": "system", "content": "You are Hubble."},
        {"role": "user", "content": [{"type": "text", "text": "look"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]},
        {"role": "assistant", "content": "checking", "tool_calls": [
            {"id": "call:1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
            {"id": "call_2", "type": "function", "function": {"name": "grep", "arguments": '{"pattern": "x"}'}}]},
        {"role": "tool", "tool_call_id": "call:1", "content": "file a"},
        {"role": "tool", "tool_call_id": "call_2", "content": "Error: bad regex"},
        {"role": "user", "content": "thanks"},
    ]
    system, msgs = to_anthropic(hist)
    assert system == "You are Hubble."
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"][1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                                "data": "AAAA"}}
    uses = [b for b in msgs[1]["content"] if b["type"] == "tool_use"]
    assert uses[0]["id"] == "call_1" and uses[0]["input"] == {"path": "a"}  # id sanitized, args parsed
    results = [b for b in msgs[2]["content"] if b["type"] == "tool_result"]
    assert len(results) == 2 and results[1]["is_error"] is True               # one user turn, both results
    assert msgs[2]["content"][-1] == {"type": "text", "text": "thanks"}


def test_raw_blocks_echoed_back_and_hidden_from_openai_providers():
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "hi"}]
    hist = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "hi", "_anthropic_content": raw}]
    assert to_anthropic(hist)[1][1]["content"] == raw
    assert "_anthropic_content" not in normalize_messages(hist)[1]


def test_tool_schema_conversion():
    tools = to_anthropic_tools([{"type": "function", "function": {
        "name": "shell", "description": "run", "parameters": {"type": "object", "properties": {}}}}])
    assert tools == [{"name": "shell", "description": "run", "input_schema": {"type": "object", "properties": {}},
                      "eager_input_streaming": True}]


def test_base_url_without_v1():
    assert anthropic_base_url("https://api.anthropic.com/v1") == "https://api.anthropic.com"
    assert anthropic_base_url("https://proxy.example.com/anthropic") == "https://proxy.example.com/anthropic"


@pytest.mark.parametrize("fake", [[text_reply("Hello there")]], indirect=True)
def test_stream_text_usage_and_headers(fake):
    p = AnthropicProvider(fake.url, "sk-ant-test")
    seen = []
    r = p.stream("claude-haiku-4-5", [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                 on_text=seen.append)
    assert r.text == "Hello there" and "".join(seen) == "Hello there"
    assert r.finish_reason == "stop"
    assert r.usage == {"prompt_tokens": 150, "completion_tokens": 42, "total_tokens": 192}
    req, hdr = fake.requests[0], {k.lower(): v for k, v in fake.headers[0].items()}
    assert hdr["x-api-key"] == "sk-ant-test" and "anthropic-version" in hdr
    assert hdr["user-agent"].startswith("hubble-cli/")
    assert req["system"][0]["text"] == "sys" and "temperature" not in req
    assert req["max_tokens"] >= 32000


@pytest.mark.parametrize("fake", [[
    tool_reply("toolu_1", "read_file", {"path": "a.txt"}, text="Reading.", thinking=""),
    text_reply("It says hello."),
]], indirect=True)
def test_agent_tool_loop_over_claude(fake, tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    agent = Agent(AnthropicProvider(fake.url, "k"), dict(SETTINGS), ToolContext(root=tmp_path), Permissions(),
                  Events())
    assert agent.run("what is in a.txt") == "It says hello."
    second = fake.requests[1]["messages"]
    assistant = second[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-toolu_1"}  # verbatim
    tool_result = second[2]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert "hello" in tool_result["content"]
    assert {t["name"] for t in fake.requests[0]["tools"]} >= {"read_file", "write_file", "shell"}


@pytest.mark.parametrize("fake", [[{"blocks": [{"type": "tool_use", "id": "toolu_9", "name": "write_file",
                                                "input": {"path": "x"}}], "stop": "max_tokens"}]], indirect=True)
def test_truncated_tool_call_is_not_run(fake):
    r = AnthropicProvider(fake.url, "k").stream("claude-opus-5", [{"role": "user", "content": "go"}])
    assert r.finish_reason == "length" and r.tool_calls == []


def test_list_models_and_auth_error():
    s = FakeAnthropic([])
    try:
        models = AnthropicProvider(s.url, "k").list_models()
        assert {m["id"]: m["context_length"] for m in models} == {"claude-opus-5": 1000000,
                                                                   "claude-haiku-4-5": 200000}
    finally:
        s.close()
    bad = FakeAnthropic([], status=401)
    try:
        with pytest.raises(ProviderError) as e:
            AnthropicProvider(bad.url, "wrong", max_retries=0).stream("claude-opus-5",
                                                                      [{"role": "user", "content": "x"}])
        assert "401" in str(e.value) and not e.value.transient
    finally:
        bad.close()


def test_fallbacks_only_on_first_party_for_supported_models():
    p = AnthropicProvider("https://api.anthropic.com", "k")
    assert p._use_fallbacks("claude-opus-5") and p._use_fallbacks("claude-fable-5-1")
    assert not p._use_fallbacks("claude-haiku-4-5")
    assert not AnthropicProvider("https://proxy.example.com", "k")._use_fallbacks("claude-opus-5")


def test_provider_config_kind_detection_and_env(monkeypatch, tmp_path):
    import hubble.providers as pv
    monkeypatch.setattr(pv, "PROVIDERS_FILE", tmp_path / "providers.json")
    monkeypatch.setattr(pv, "HOME_DIR", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    loaded = pv.load_providers({})
    assert loaded["anthropic"].kind == "anthropic" and loaded["anthropic"].api_key == "sk-ant-env"
    assert isinstance(pv.make_client(loaded["anthropic"]), AnthropicProvider)
    pv.save_provider(pv.ProviderConfig("claude", "https://api.anthropic.com/v1", "k2", False, "anthropic"))
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    again = pv.load_providers({})
    assert again["claude"].kind == "anthropic" and "anthropic" not in again
    assert pv.detect_kind("https://api.openai.com/v1") == "openai"


def test_scanner_uses_model_listing_for_claude(tmp_path):
    from hubble.scanner import ModelScanner
    s = FakeAnthropic([])
    try:
        out = tmp_path / "claude.json"
        sc = ModelScanner(s.url, "k", output=out, kind="anthropic")
        sc._run()
        data = json.loads(out.read_text(encoding="utf-8"))
        assert sc.status == "done" and data["working_count"] == 2
        assert {m["model"]: m["context_length"] for m in data["working_models"]}["claude-opus-5"] == 1000000
        assert s.requests == []  # listing only: no paid probe requests
    finally:
        s.close()
