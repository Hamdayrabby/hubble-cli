"""MCP (Model Context Protocol) client: stdio, Streamable HTTP and legacy SSE transports.

Connects to MCP servers, performs the initialize handshake, and exposes what they offer:
  - tools      -> normal Tools the agent calls like any built-in one (mcp__<server>__<tool>)
  - resources  -> two extra tools per server: list_resources and read_resource
  - prompts    -> slash commands in the REPL (/mcp__<server>__<prompt>)

Config (settings.json "mcp_servers"):
  local:   {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."], "env": {...}}
  remote:  {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}}
           optional "transport": "http" | "sse" | "auto" (default auto: Streamable HTTP, falling
           back to the older HTTP+SSE transport); optional "oauth": {"client_id", "client_secret",
           "scopes"}. A remote server that answers 401 is logged into with OAuth (/mcp login <name>).
`${VAR}` in headers, env and url is read from the environment.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx

from hubble.tools import Tool, ToolContext, ToolError

PROTOCOL_VERSION = "2025-06-18"
ENV_RX = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MCPError(Exception):
    pass


class MCPAuthRequired(MCPError):
    """The server wants an OAuth login. `www_authenticate` is the 401's header, if any."""

    def __init__(self, message: str, www_authenticate: str = ""):
        super().__init__(message)
        self.www_authenticate = www_authenticate


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return ENV_RX.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


@dataclass
class MCPServerConfig:
    name: str
    command: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    transport: str = "auto"          # for url servers: auto | http | sse
    oauth: Dict[str, Any] = field(default_factory=dict)


# ----- transports -----------------------------------------------------------

class _StdioTransport:
    def __init__(self, config: MCPServerConfig, timeout: float):
        self.config = config
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None

    def open(self):
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

    def close(self):
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

    def _write(self, obj: Dict[str, Any]):
        if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
            raise MCPError(f"MCP server '{self.config.name}' is not running")
        try:
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise MCPError(f"MCP server '{self.config.name}' closed its input: {e}") from None

    def notify(self, msg: Dict[str, Any]):
        self._write(msg)

    def request(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        self._write(msg)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line == "":
                stderr = self.proc.stderr.read(4000) if self.proc.stderr else ""
                raise MCPError(f"MCP server '{self.config.name}' exited unexpectedly during "
                               f"'{msg['method']}'.{(' stderr: ' + stderr.strip()) if stderr.strip() else ''}")
            line = line.strip()
            if not line:
                continue
            try:
                reply = json.loads(line)
            except ValueError:
                continue  # a non-JSON-RPC log line on stdout; ignore and keep reading
            if isinstance(reply, dict) and reply.get("id") == msg["id"] and "method" not in reply:
                return reply
        raise MCPError(f"MCP server '{self.config.name}' timed out on '{msg['method']}' after {self.timeout}s")


def _sse_events(lines):
    """Parse a text/event-stream into (event, data) pairs."""
    event, data = "message", []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
        elif line.startswith(":"):
            continue
        else:
            key, _, val = line.partition(":")
            val = val[1:] if val.startswith(" ") else val
            if key == "event":
                event = val
            elif key == "data":
                data.append(val)
    if data:
        yield event, "\n".join(data)


class _HttpTransport:
    """Streamable HTTP (MCP 2025-03-26+): every message is a POST; the reply is JSON or an SSE stream."""

    def __init__(self, config: MCPServerConfig, timeout: float, auth_header=None, http: Optional[httpx.Client] = None):
        self.config = config
        self.timeout = timeout
        self.auth_header = auth_header or (lambda: {})
        self.http = http or httpx.Client(timeout=timeout, follow_redirects=True)
        self.session_id = ""
        self.protocol = ""

    def open(self):
        pass

    def close(self):
        if self.session_id:
            try:
                self.http.delete(self.config.url, headers=self._headers(), timeout=5)
            except httpx.HTTPError:
                pass
        self.http.close()

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             **self.config.headers, **self.auth_header()}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol:
            h["MCP-Protocol-Version"] = self.protocol
        return h

    def _post(self, msg: Dict[str, Any]) -> httpx.Response:
        try:
            req = self.http.build_request("POST", self.config.url, headers=self._headers(), json=msg)
            resp = self.http.send(req, stream=True)
        except httpx.HTTPError as e:
            raise MCPError(f"MCP server '{self.config.name}' unreachable: {type(e).__name__}: {e}") from None
        if resp.status_code == 401:
            www = resp.headers.get("www-authenticate", "")
            resp.close()
            raise MCPAuthRequired(f"MCP server '{self.config.name}' needs a login (HTTP 401)", www)
        if resp.status_code >= 400:
            body = resp.read().decode("utf-8", "replace")[:300]
            resp.close()
            err = MCPError(f"MCP server '{self.config.name}' returned HTTP {resp.status_code}: {body}")
            err.status_code = resp.status_code
            raise err
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        return resp

    def notify(self, msg: Dict[str, Any]):
        self._post(msg).close()

    def request(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._post(msg)
        try:
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                for _, data in _sse_events(resp.iter_lines()):
                    reply = _match(data, msg["id"])
                    if reply is not None:
                        return reply
                raise MCPError(f"MCP server '{self.config.name}' closed the stream without answering "
                               f"'{msg['method']}'")
            reply = _match(resp.read().decode("utf-8", "replace"), msg["id"])
            if reply is None:
                raise MCPError(f"MCP server '{self.config.name}' sent no reply to '{msg['method']}'")
            return reply
        finally:
            resp.close()


def _match(data: str, req_id) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    for item in obj if isinstance(obj, list) else [obj]:
        if isinstance(item, dict) and item.get("id") == req_id and "method" not in item:
            return item
    return None


class _SseTransport:
    """The older HTTP+SSE transport (MCP 2024-11-05): a long-lived GET stream carries every reply;
    requests are POSTed to the endpoint the stream announces first."""

    def __init__(self, config: MCPServerConfig, timeout: float, auth_header=None, http: Optional[httpx.Client] = None):
        self.config = config
        self.timeout = timeout
        self.auth_header = auth_header or (lambda: {})
        self.http = http or httpx.Client(timeout=httpx.Timeout(timeout, read=None), follow_redirects=True)
        self.endpoint = ""
        self.replies: Dict[Any, Dict[str, Any]] = {}
        self.cond = threading.Condition()
        self.error: Optional[Exception] = None
        self._resp = None
        self._ready = threading.Event()

    def _headers(self, accept="application/json"):
        return {"Accept": accept, **self.config.headers, **self.auth_header()}

    def open(self):
        try:
            req = self.http.build_request("GET", self.config.url, headers=self._headers("text/event-stream"))
            self._resp = self.http.send(req, stream=True)
        except httpx.HTTPError as e:
            raise MCPError(f"MCP server '{self.config.name}' unreachable: {type(e).__name__}: {e}") from None
        if self._resp.status_code == 401:
            www = self._resp.headers.get("www-authenticate", "")
            self._resp.close()
            raise MCPAuthRequired(f"MCP server '{self.config.name}' needs a login (HTTP 401)", www)
        if self._resp.status_code >= 400:
            self._resp.close()
            raise MCPError(f"MCP server '{self.config.name}' SSE stream: HTTP {self._resp.status_code}")
        threading.Thread(target=self._reader, daemon=True, name=f"mcp-sse-{self.config.name}").start()
        if not self._ready.wait(self.timeout) or not self.endpoint:
            raise MCPError(f"MCP server '{self.config.name}' never announced its message endpoint")

    def _reader(self):
        try:
            for event, data in _sse_events(self._resp.iter_lines()):
                if event == "endpoint":
                    self.endpoint = urljoin(self.config.url, data.strip())
                    self._ready.set()
                    continue
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                with self.cond:
                    for item in obj if isinstance(obj, list) else [obj]:
                        if isinstance(item, dict) and "id" in item and "method" not in item:
                            self.replies[item["id"]] = item
                    self.cond.notify_all()
        except Exception as e:  # stream dropped
            self.error = e
        finally:
            self._ready.set()
            with self.cond:
                self.cond.notify_all()

    def close(self):
        try:
            if self._resp is not None:
                self._resp.close()
        finally:
            self.http.close()

    def _post(self, msg):
        try:
            resp = self.http.post(self.endpoint, headers={**self._headers(), "Content-Type": "application/json"},
                                  json=msg, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise MCPError(f"MCP server '{self.config.name}' unreachable: {type(e).__name__}: {e}") from None
        if resp.status_code >= 400:
            raise MCPError(f"MCP server '{self.config.name}' returned HTTP {resp.status_code}")

    def notify(self, msg):
        self._post(msg)

    def request(self, msg):
        self._post(msg)
        deadline = time.time() + self.timeout
        with self.cond:
            while msg["id"] not in self.replies:
                left = deadline - time.time()
                if left <= 0:
                    raise MCPError(f"MCP server '{self.config.name}' timed out on '{msg['method']}'")
                if self.error is not None:
                    raise MCPError(f"MCP server '{self.config.name}' stream closed: {self.error}")
                self.cond.wait(min(left, 0.5))
            return self.replies.pop(msg["id"])


# ----- client ---------------------------------------------------------------

class MCPClient:
    """One connection to one MCP server. Calls are serialized with a lock."""

    def __init__(self, config: MCPServerConfig, timeout: float = 20.0, http: Optional[httpx.Client] = None):
        self.config = config
        self.timeout = timeout
        self._http = http  # injectable for tests
        self._transport = None
        self._id = 0
        self._lock = threading.Lock()
        self.server_info: Dict[str, Any] = {}
        self.capabilities: Dict[str, Any] = {}
        self.tools: List[Dict[str, Any]] = []
        self.prompts: List[Dict[str, Any]] = []
        self.token_store = None  # set for url servers: hubble.mcp_oauth.TokenStore

    @property
    def proc(self):  # back-compat for code/tests that look at the stdio subprocess
        return getattr(self._transport, "proc", None)

    def _auth_header(self) -> Dict[str, str]:
        if self.token_store is not None:
            token = self.token_store.access_token(self.config.name)
            if token:
                return {"Authorization": f"Bearer {token}"}
        return {}

    def _make_transport(self, kind: str):
        if not self.config.url:
            return _StdioTransport(self.config, self.timeout)
        cls = _SseTransport if kind == "sse" else _HttpTransport
        return cls(self.config, self.timeout, self._auth_header, self._http)

    def start(self):
        kinds = ["stdio"] if not self.config.url else (
            [self.config.transport] if self.config.transport in ("http", "sse") else ["http", "sse"])
        last: Optional[Exception] = None
        for kind in kinds:
            self._transport = self._make_transport(kind)
            try:
                self._transport.open()
                self._handshake()
                return
            except MCPAuthRequired:
                self._close_transport()
                raise
            except MCPError as e:
                self._close_transport()
                last = e
                # Only fall back to SSE when the server clearly is not a Streamable HTTP endpoint.
                if kind == "http" and getattr(e, "status_code", 0) not in (400, 404, 405):
                    break
        raise last or MCPError(f"could not connect to MCP server '{self.config.name}'")

    def _handshake(self):
        resp = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "hubble", "version": "1"},
        })
        self.server_info = resp.get("serverInfo", {})
        self.capabilities = resp.get("capabilities", {}) or {}
        if isinstance(self._transport, _HttpTransport):
            self._transport.protocol = resp.get("protocolVersion", PROTOCOL_VERSION)
        self._notify("notifications/initialized", {})
        self.tools = self._request("tools/list", {}).get("tools", []) if self._has("tools", default=True) else []
        self.prompts = []
        if self._has("prompts"):
            try:
                self.prompts = self._request("prompts/list", {}).get("prompts", [])
            except MCPError:
                pass

    def _has(self, cap: str, default: bool = False) -> bool:
        if not self.capabilities:
            return default
        return cap in self.capabilities

    @property
    def has_resources(self) -> bool:
        return self._has("resources")

    def _close_transport(self):
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None

    def stop(self):
        self._close_transport()

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        result = self._request("tools/call", {"name": name, "arguments": arguments})
        text = _content_text(result.get("content", []))
        if result.get("isError"):
            raise ToolError(text)
        return text

    def list_resources(self) -> List[Dict[str, Any]]:
        return self._request("resources/list", {}).get("resources", [])

    def read_resource(self, uri: str) -> str:
        result = self._request("resources/read", {"uri": uri})
        parts = []
        for c in result.get("contents", []) or []:
            if "text" in c:
                parts.append(c["text"])
            else:
                parts.append(f"[{c.get('mimeType', 'binary')} content, {len(c.get('blob', ''))} base64 chars omitted]")
        return "\n".join(parts) or "(empty resource)"

    def get_prompt(self, name: str, arguments: Dict[str, str]) -> str:
        result = self._request("prompts/get", {"name": name, "arguments": arguments})
        texts = []
        for m in result.get("messages", []) or []:
            c = m.get("content")
            texts.append(_content_text(c if isinstance(c, list) else [c] if c else []))
        return "\n\n".join(t for t in texts if t)

    # ----- JSON-RPC plumbing ---------------------------------------------

    def _notify(self, method: str, params: Dict[str, Any]):
        if self._transport is None:
            raise MCPError(f"MCP server '{self.config.name}' is not running")
        self._transport.notify({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if self._transport is None:
                raise MCPError(f"MCP server '{self.config.name}' is not running")
            self._id += 1
            reply = self._transport.request({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        if "error" in reply:
            err = reply["error"]
            raise MCPError(f"{self.config.name}.{method}: {err.get('message', err) if isinstance(err, dict) else err}")
        return reply.get("result", {}) or {}


def _content_text(blocks) -> str:
    parts = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif block.get("type") == "resource" and isinstance(block.get("resource"), dict) and "text" in block["resource"]:
            parts.append(block["resource"]["text"])
        else:
            parts.append(f"[{block.get('type', 'unknown')} content omitted]")
    return "\n".join(parts) or "(empty result)"


# ----- tools ----------------------------------------------------------------

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


class MCPListResources(MCPTool):
    kind = "read"  # reading data the user connected; no side effects

    def __init__(self, client: MCPClient):
        self.client = client
        self.tool_name = "list_resources"
        self.name = f"mcp__{client.config.name}__list_resources"
        self.description = f"List the resources (files, records, docs) MCP server '{client.config.name}' offers."
        self.parameters = {"type": "object", "properties": {}}

    def run(self, args, ctx):
        items = self.client.list_resources()
        if not items:
            return "(no resources)"
        return "\n".join(f"{r.get('uri')}  {r.get('name', '')}  {r.get('description', '') or ''}".rstrip()
                         for r in items[:500])


class MCPReadResource(MCPTool):
    kind = "read"

    def __init__(self, client: MCPClient):
        self.client = client
        self.tool_name = "read_resource"
        self.name = f"mcp__{client.config.name}__read_resource"
        self.description = f"Read one resource from MCP server '{client.config.name}' by URI (see list_resources)."
        self.parameters = {"type": "object", "properties": {"uri": {"type": "string"}}, "required": ["uri"]}

    def target(self, args):
        return f"{self.client.config.name}:{args.get('uri', '')}"

    def run(self, args, ctx):
        from hubble.tools import truncate
        return truncate(self.client.read_resource(args["uri"]), 60000)


def tools_for(client: MCPClient) -> List[Tool]:
    out: List[Tool] = [MCPTool(client, spec) for spec in client.tools]
    if client.has_resources:
        out += [MCPListResources(client), MCPReadResource(client)]
    return out


def config_from(name: str, entry: Dict[str, Any], root) -> MCPServerConfig:
    entry = expand_env(entry)
    return MCPServerConfig(
        name=name, command=[str(c) for c in entry.get("command") or []],
        env={str(k): str(v) for k, v in (entry.get("env") or {}).items()}, cwd=str(root),
        url=str(entry.get("url") or ""), headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
        transport=str(entry.get("transport") or "auto").lower(), oauth=dict(entry.get("oauth") or {}))


# Servers that answered 401 at startup, waiting for /mcp login: name -> (config, www-authenticate).
PENDING_LOGIN: Dict[str, Any] = {}


def connect(name: str, entry: Dict[str, Any], root, events=None) -> Optional[MCPClient]:
    """Start one server. Returns None (with a notice) if it is misconfigured, down, or needs a login."""
    url = entry.get("url")
    command = entry.get("command")
    if not url and (not command or not isinstance(command, list)):
        if events:
            events.notice(f"mcp_servers.{name}: needs a 'url', or a 'command' that must be a list of strings; "
                          "skipped.", "warn")
        return None
    client = MCPClient(config_from(name, entry, root))
    if client.config.url:
        from hubble.mcp_oauth import TokenStore
        client.token_store = TokenStore()
    try:
        client.start()
    except MCPAuthRequired as e:
        if client.token_store is not None and client.token_store.refresh(client.config, e.www_authenticate):
            try:
                client.start()
                PENDING_LOGIN.pop(name, None)
                return client
            except MCPError:
                pass
        PENDING_LOGIN[name] = (client.config, e.www_authenticate)
        if events:
            events.notice(f"MCP server '{name}' needs a login: run /mcp login {name}", "warn")
        return None
    except MCPError as e:
        if events:
            events.notice(f"MCP server '{name}' unavailable: {e}", "warn")
        return None
    PENDING_LOGIN.pop(name, None)
    return client


def load_mcp_servers(settings: Dict[str, Any], root, events=None) -> List[Tool]:
    """Start every configured MCP server and return the tools it offers. A server that fails to
    start or hand shake is skipped with a notice, not a crash -- the rest of the CLI still works."""
    tools: List[Tool] = []
    for name, entry in (settings.get("mcp_servers") or {}).items():
        if not isinstance(entry, dict):
            continue
        client = connect(name, entry, root, events)
        if client is None:
            continue
        server_tools = tools_for(client)
        tools += server_tools
        if events:
            extra = f", {len(client.prompts)} prompt(s)" if client.prompts else ""
            events.notice(f"MCP server '{name}' connected: {len(client.tools)} tool(s){extra}.", "dim")
    return tools


def mcp_clients(tools: List[Tool]) -> List[MCPClient]:
    seen, out = set(), []
    for t in tools:
        if isinstance(t, MCPTool) and id(t.client) not in seen:
            seen.add(id(t.client))
            out.append(t.client)
    return out


def stop_mcp_clients(tools: List[Tool]):
    for c in mcp_clients(tools):
        c.stop()
