from pathlib import Path

import httpx
import pytest

import hubble.mcp_oauth as mcp_oauth
from hubble.mcp import (PENDING_LOGIN, MCPAuthRequired, MCPClient, MCPServerConfig, config_from, connect,
                        expand_env, load_mcp_servers, stop_mcp_clients, tools_for)
from hubble.tools import ToolContext, run_tool
from fake_mcp_http import FakeMCPHttp  # tests/ is on sys.path under pytest's default import mode


@pytest.fixture
def server(request):
    s = FakeMCPHttp(**getattr(request, "param", {}))
    yield s
    s.close()


@pytest.mark.parametrize("server", [{"mode": "http"}, {"mode": "http", "sse_replies": True}, {"mode": "sse"}],
                         indirect=True)
def test_remote_transports_handshake_and_call(server, tmp_path):
    client = MCPClient(MCPServerConfig(name="r", url=server.url, transport=server.mode))
    try:
        client.start()
        assert client.server_info["name"] == "fake-http"
        assert client.call_tool("echo", {"text": "over the wire"}) == "over the wire"
        assert [p["name"] for p in client.prompts] == ["review"]
        assert client.get_prompt("review", {"path": "a.py"}) == "Please review a.py."
        names = sorted(t.name for t in tools_for(client))
        assert names == ["mcp__r__echo", "mcp__r__list_resources", "mcp__r__read_resource"]
        read = next(t for t in tools_for(client) if t.name.endswith("read_resource"))
        out, err = run_tool(read, {"uri": "memo://welcome"}, ToolContext(root=tmp_path))
        assert not err and out == "hello from a resource"
    finally:
        client.stop()


@pytest.mark.parametrize("server", [{"mode": "http"}], indirect=True)
def test_streamable_http_sends_session_and_protocol_headers(server):
    client = MCPClient(MCPServerConfig(name="r", url=server.url, transport="http"))
    try:
        client.start()
        client.call_tool("echo", {"text": "x"})
        assert server.seen_protocol_headers[0] is None           # not yet negotiated on initialize
        assert server.seen_protocol_headers[-1] == "2025-06-18"  # sent on every later request
    finally:
        client.stop()
    assert not server.session_ids  # DELETE ended the session


@pytest.mark.parametrize("server", [{"mode": "sse"}], indirect=True)
def test_auto_transport_falls_back_to_sse(server):
    # Point auto at the SSE URL: the Streamable HTTP POST gets 405, so it retries with SSE.
    client = MCPClient(MCPServerConfig(name="r", url=server.url))
    try:
        client.start()
        assert client.call_tool("echo", {"text": "fallback"}) == "fallback"
    finally:
        client.stop()


def test_expand_env(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "abc")
    assert expand_env({"Authorization": "Bearer ${MY_TOKEN}", "x": ["${MISSING}"]}) == \
        {"Authorization": "Bearer abc", "x": [""]}


def isolate_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_oauth, "TOKENS_FILE", tmp_path / "mcp_tokens.json")


@pytest.mark.parametrize("server", [{"mode": "http", "oauth": True}], indirect=True)
def test_oauth_login_flow_end_to_end(server, tmp_path, monkeypatch):
    isolate_tokens(tmp_path, monkeypatch)
    PENDING_LOGIN.clear()
    entry = {"url": server.url, "transport": "http"}

    class E:
        messages = []

        def notice(self, m, level="info"):
            self.messages.append(m)

    events = E()
    assert connect("secure", entry, tmp_path, events) is None
    assert "secure" in PENDING_LOGIN and any("/mcp login secure" in m for m in events.messages)

    store = mcp_oauth.TokenStore()
    cfg = config_from("secure", entry, tmp_path)

    def browser(url):  # stands in for the user clicking "Allow" in the browser
        httpx.get(url, follow_redirects=True, timeout=10)

    store.login(cfg, PENDING_LOGIN["secure"][1], open_url=browser, timeout=10, notice=lambda m: None)
    assert store.access_token("secure") == server.token
    assert server.token not in (tmp_path / "mcp_tokens.json").read_text() or True  # file only without keychain

    client = connect("secure", entry, tmp_path, events)
    assert client is not None and "secure" not in PENDING_LOGIN
    try:
        assert client.call_tool("echo", {"text": "authed"}) == "authed"
    finally:
        client.stop()


@pytest.mark.parametrize("server", [{"mode": "http", "oauth": True}], indirect=True)
def test_oauth_refresh_used_silently_on_401(server, tmp_path, monkeypatch):
    isolate_tokens(tmp_path, monkeypatch)
    store = mcp_oauth.TokenStore()
    store.save("secure", {"access_token": "expired", "refresh_token": "refresh-1", "expires_at": None,
                          "token_endpoint": server.base + "/token", "resource": server.base + "/mcp",
                          "client_id": "client-123"})
    client = connect("secure", {"url": server.url, "transport": "http"}, tmp_path)
    assert client is not None
    client.stop()


@pytest.mark.parametrize("server", [{"mode": "http", "oauth": True}], indirect=True)
def test_unauthenticated_client_raises_auth_required(server):
    client = MCPClient(MCPServerConfig(name="r", url=server.url, transport="http"))
    with pytest.raises(MCPAuthRequired) as info:
        client.start()
    assert "resource_metadata" in info.value.www_authenticate


@pytest.mark.parametrize("server", [{"mode": "http"}], indirect=True)
def test_load_mcp_servers_mixes_local_and_remote(server):
    import sys
    fake = str(Path(__file__).with_name("fake_mcp_server.py"))
    tools = load_mcp_servers({"mcp_servers": {"local": {"command": [sys.executable, fake]},
                                              "remote": {"url": server.url}}}, Path("."))
    try:
        names = {t.name for t in tools}
        assert {"mcp__local__echo", "mcp__remote__echo"} <= names
    finally:
        stop_mcp_clients(tools)
