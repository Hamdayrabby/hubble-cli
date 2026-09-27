"""MCP (Model Context Protocol) client: stdio transport only.

Connects to external MCP servers (spawned as subprocesses), performs the initialize handshake,
lists their tools, and wraps each one as a normal Tool so the agent loop calls it exactly like
any built-in tool. SSE/HTTP transports are not implemented; stdio covers the reference servers
(filesystem, git, fetch, etc.) and most third-party ones.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from hubble.tools import Tool, ToolContext, ToolError

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    pass


@dataclass
class MCPServerConfig:
    name: str
    command: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None


class MCPClient:
    """One connection to one MCP server over stdio. Not thread-safe for concurrent calls."""

    def __init__(self, config: MCPServerConfig, timeout: float = 20.0):
        self.config = config
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None
        self._id = 0
        self._lock = threading.Lock()
        self.server_info: Dict[str, Any] = {}
        self.tools: List[Dict[str, Any]] = []

    def start(self):
        env = dict(os.environ, **self.config.env)
        command = list(self.config.command)
        if sys.platform == "win32" and command:
            # npx/npm/yarn/pnpm etc. are .cmd shims on Windows; Popen without shell=True only
            # resolves a bare exe name through PATH, not the .cmd/.bat/.ps1 extensions in
            # PATHEXT, so a plain ["npx", ...] command silently fails to launch at all.
            resolved = shutil.which(command[0])
            if resolved:
                command[0] = resolved
        try:
            self.proc = subprocess.Popen(
                command, cwd=self.config.cwd, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            raise MCPError(f"could not start MCP server '{self.config.name}': {e}") from None
        try:
            resp = self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "hubble", "version": "1"},
            })
        except MCPError:
            self.stop()
            raise
        self.server_info = resp.get("serverInfo", {})
        self._notify("notifications/initialized", {})
        listed = self._request("tools/list", {})
        self.tools = listed.get("tools", [])

    def stop(self):
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        parts = []
        for block in result.get("content", []) or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                parts.append(f"[{block.get('type', 'unknown')} content omitted]")
        text = "\n".join(parts) or "(empty result)"
        if result.get("isError"):
            raise ToolError(text)
        return text

    # ----- JSON-RPC plumbing ---------------------------------------------

    def _write(self, obj: Dict[str, Any]):
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            raise MCPError(f"MCP server '{self.config.name}' is not running")
        line = json.dumps(obj, ensure_ascii=False)
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise MCPError(f"MCP server '{self.config.name}' closed its input: {e}") from None

    def _notify(self, method: str, params: Dict[str, Any]):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._id += 1
            req_id = self._id
            self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                line = self.proc.stdout.readline()
                if line == "":
                    stderr = self.proc.stderr.read(4000) if self.proc.stderr else ""
                    raise MCPError(f"MCP server '{self.config.name}' exited unexpectedly during "
                                   f"'{method}'.{(' stderr: ' + stderr.strip()) if stderr.strip() else ''}")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # a non-JSON-RPC log line on stdout; ignore and keep reading
                if msg.get("id") != req_id:
                    continue  # a notification, or a response to a call we're not making
                if "error" in msg:
                    err = msg["error"]
                    raise MCPError(f"{self.config.name}.{method}: {err.get('message', err)}")
                return msg.get("result", {})
            raise MCPError(f"MCP server '{self.config.name}' timed out on '{method}' "
                           f"after {self.timeout}s")


class MCPTool(Tool):
    kind = "mcp"

    def __init__(self, client: MCPClient, spec: Dict[str, Any]):
        self.client = client
        self.tool_name = spec["name"]
        self.name = f"mcp__{client.config.name}__{spec['name']}"
        self.description = (spec.get("description") or f"Tool '{spec['name']}' from MCP server "
                            f"'{client.config.name}'.")[:1024]
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        self.parameters = schema

    def target(self, args: Dict[str, Any]) -> str:
        return f"{self.client.config.name}.{self.tool_name}"

    def preview(self, args, ctx):
        return json.dumps(args, ensure_ascii=False)[:2000]

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        return self.client.call_tool(self.tool_name, args)


def load_mcp_servers(settings: Dict[str, Any], root, events=None) -> List[MCPTool]:
    """Start every configured MCP server and return the tools it offers. A server that fails to
    start or hand shake is skipped with a notice, not a crash -- the rest of the CLI still works."""
    configs = settings.get("mcp_servers") or {}
    tools: List[MCPTool] = []
    for name, entry in configs.items():
        command = entry.get("command")
        if not command or not isinstance(command, list):
            if events:
                events.notice(f"mcp_servers.{name}: 'command' must be a list of strings; skipped.", "warn")
            continue
        client = MCPClient(MCPServerConfig(name=name, command=[str(c) for c in command],
                                          env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
                                          cwd=str(root)))
        try:
            client.start()
        except MCPError as e:
            if events:
                events.notice(f"MCP server '{name}' unavailable: {e}", "warn")
            continue
        for spec in client.tools:
            tools.append(MCPTool(client, spec))
        if events:
            events.notice(f"MCP server '{name}' connected: {len(client.tools)} tool(s).", "dim")
    return tools


def stop_mcp_clients(tools: List[Tool]):
    seen = set()
    for t in tools:
        if isinstance(t, MCPTool) and id(t.client) not in seen:
            seen.add(id(t.client))
            t.client.stop()
