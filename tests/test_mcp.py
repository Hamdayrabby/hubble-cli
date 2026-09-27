import sys
from pathlib import Path

import pytest

from hubble.mcp import MCPClient, MCPError, MCPServerConfig, MCPTool, load_mcp_servers, stop_mcp_clients
from hubble.tools import ToolContext, run_tool

FAKE_SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))


def _config(name="fake", extra_args=()):
    return MCPServerConfig(name=name, command=[sys.executable, FAKE_SERVER, *extra_args])


def test_handshake_and_list_tools():
    client = MCPClient(_config())
    try:
        client.start()
        assert client.server_info.get("name") == "fake"
        names = {t["name"] for t in client.tools}
        assert names == {"echo", "boom"}
    finally:
        client.stop()


def test_call_tool_success_and_failure():
    client = MCPClient(_config())
    try:
        client.start()
        assert client.call_tool("echo", {"text": "hello"}) == "hello"
        from hubble.tools import ToolError
        with pytest.raises(ToolError, match="failed on purpose"):
            client.call_tool("boom", {})
    finally:
        client.stop()


def test_unknown_method_error_surfaces():
    client = MCPClient(_config())
    try:
        client.start()
        with pytest.raises(MCPError):
            client._request("not/a/real/method", {})
    finally:
        client.stop()


def test_server_crash_mid_call_raises_clean_error():
    client = MCPClient(_config(extra_args=["--crash-on-call"]))
    client.start()
    try:
        with pytest.raises(MCPError, match="exited unexpectedly"):
            client.call_tool("echo", {"text": "x"})
    finally:
        client.stop()


def test_missing_executable_raises_at_start():
    client = MCPClient(MCPServerConfig(name="nope", command=["this-binary-does-not-exist-anywhere"]))
    with pytest.raises(MCPError):
        client.start()


def test_mcp_tool_wraps_schema_and_namespaces_name():
    client = MCPClient(_config())
    try:
        client.start()
        spec = next(t for t in client.tools if t["name"] == "echo")
        tool = MCPTool(client, spec)
        assert tool.name == "mcp__fake__echo"
        assert tool.parameters["required"] == ["text"]
        assert tool.target({"text": "x"}) == "fake.echo"
    finally:
        client.stop()


def test_mcp_tool_runs_through_run_tool(tmp_path):
    client = MCPClient(_config())
    try:
        client.start()
        spec = next(t for t in client.tools if t["name"] == "echo")
        tool = MCPTool(client, spec)
        ctx = ToolContext(root=tmp_path)
        out, err = run_tool(tool, {"text": "via run_tool"}, ctx)
        assert not err and out == "via run_tool"
    finally:
        client.stop()


class _Events:
    def __init__(self):
        self.messages = []

    def notice(self, message, level="info"):
        self.messages.append((message, level))


def test_load_mcp_servers_connects_and_names_tools():
    settings = {"mcp_servers": {"fake": {"command": [sys.executable, FAKE_SERVER]}}}
    events = _Events()
    tools = load_mcp_servers(settings, Path("."), events)
    try:
        names = sorted(t.name for t in tools)
        assert names == ["mcp__fake__boom", "mcp__fake__echo"]
        assert any("connected: 2 tool" in m for m, _ in events.messages)
    finally:
        stop_mcp_clients(tools)


def test_load_mcp_servers_skips_broken_server_without_crashing():
    settings = {"mcp_servers": {"bad": {"command": ["this-binary-does-not-exist-anywhere"]},
                               "good": {"command": [sys.executable, FAKE_SERVER]}}}
    events = _Events()
    tools = load_mcp_servers(settings, Path("."), events)
    try:
        assert any(t.name.startswith("mcp__good__") for t in tools)
        assert any(t.name.startswith("mcp__bad__") for t in tools) is False
        assert any("unavailable" in m for m, lvl in events.messages if lvl == "warn")
    finally:
        stop_mcp_clients(tools)


def test_load_mcp_servers_rejects_non_list_command():
    events = _Events()
    tools = load_mcp_servers({"mcp_servers": {"bad": {"command": "not-a-list"}}}, Path("."), events)
    assert tools == []
    assert any("must be a list" in m for m, _ in events.messages)
