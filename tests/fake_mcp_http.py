"""A small real MCP server over HTTP for tests: Streamable HTTP or legacy HTTP+SSE, optionally
behind OAuth (with its own tiny authorization server on the same port)."""

import base64
import hashlib
import json
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

TOOLS = [{"name": "echo", "description": "Echo text.",
          "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]
PROMPTS = [{"name": "review", "description": "Review a file", "arguments": [{"name": "path", "required": True}]}]
RESOURCES = [{"uri": "memo://welcome", "name": "welcome", "description": "A greeting"}]


def handle_rpc(msg):
    method, rid = msg.get("method"), msg.get("id")
    if rid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake-http", "version": "1"},
                  "capabilities": {"tools": {}, "prompts": {}, "resources": {}}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": msg["params"]["arguments"].get("text", "")}]}
    elif method == "prompts/list":
        result = {"prompts": PROMPTS}
    elif method == "prompts/get":
        path = msg["params"].get("arguments", {}).get("path", "?")
        result = {"messages": [{"role": "user", "content": {"type": "text", "text": f"Please review {path}."}}]}
    elif method == "resources/list":
        result = {"resources": RESOURCES}
    elif method == "resources/read":
        result = {"contents": [{"uri": msg["params"]["uri"], "text": "hello from a resource"}]}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


class FakeMCPHttp:
    def __init__(self, mode="http", oauth=False, sse_replies=False):
        self.mode, self.oauth, self.sse_replies = mode, oauth, sse_replies
        self.token = "tok-" + uuid.uuid4().hex[:8]
        self.codes = {}          # code -> code_challenge
        self.session_ids = set()
        self.sse_queues = {}     # legacy SSE: session -> queue of messages
        self.seen_protocol_headers = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, obj, headers=None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _authed(self):
                if not outer.oauth:
                    return True
                if self.headers.get("Authorization") == f"Bearer {outer.token}":
                    return True
                body = b"{}"
                self.send_response(401)
                self.send_header("WWW-Authenticate",
                                 f'Bearer resource_metadata="{outer.base}/.well-known/oauth-protected-resource"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return False

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n).decode()

            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                if u.path == "/.well-known/oauth-protected-resource":
                    return self._json(200, {"resource": outer.base + "/mcp", "authorization_servers": [outer.base]})
                if u.path == "/.well-known/oauth-authorization-server":
                    return self._json(200, {"issuer": outer.base, "authorization_endpoint": outer.base + "/authorize",
                                            "token_endpoint": outer.base + "/token",
                                            "registration_endpoint": outer.base + "/register"})
                if u.path == "/authorize":
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    code = uuid.uuid4().hex
                    outer.codes[code] = q["code_challenge"]
                    self.send_response(302)
                    self.send_header("Location", q["redirect_uri"] + "?" + urlencode({"code": code, "state": q["state"]}))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if u.path == "/sse" and outer.mode == "sse":
                    if not self._authed():
                        return
                    sid = uuid.uuid4().hex
                    q = outer.sse_queues[sid] = queue.Queue()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(f"event: endpoint\ndata: /messages?session={sid}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        msg = q.get()
                        if msg is None:
                            return
                        try:
                            self.wfile.write(f"event: message\ndata: {json.dumps(msg)}\n\n".encode())
                            self.wfile.flush()
                        except OSError:
                            return
                self._json(404, {"error": "not found"})

            def do_DELETE(self):  # noqa: N802
                outer.session_ids.discard(self.headers.get("Mcp-Session-Id"))
                self._json(200, {})

            def do_POST(self):  # noqa: N802
                u = urlparse(self.path)
                if u.path == "/register":
                    data = json.loads(self._body())
                    return self._json(201, {"client_id": "client-123", "redirect_uris": data["redirect_uris"]})
                if u.path == "/token":
                    form = {k: v[0] for k, v in parse_qs(self._body()).items()}
                    if form.get("grant_type") == "authorization_code":
                        challenge = outer.codes.pop(form.get("code"), None)
                        expect = base64.urlsafe_b64encode(
                            hashlib.sha256(form.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
                        if challenge != expect or form.get("resource") != outer.base + "/mcp":
                            return self._json(400, {"error": "invalid_grant"})
                    elif form.get("grant_type") == "refresh_token":
                        if form.get("refresh_token") != "refresh-1":
                            return self._json(400, {"error": "invalid_grant"})
                    return self._json(200, {"access_token": outer.token, "token_type": "Bearer",
                                            "expires_in": 3600, "refresh_token": "refresh-1"})
                if u.path == "/messages" and outer.mode == "sse":
                    sid = parse_qs(u.query)["session"][0]
                    reply = handle_rpc(json.loads(self._body()))
                    if reply:
                        outer.sse_queues[sid].put(reply)
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if u.path == "/mcp" and outer.mode == "http":
                    if not self._authed():
                        return
                    msg = json.loads(self._body())
                    outer.seen_protocol_headers.append(self.headers.get("MCP-Protocol-Version"))
                    headers = {}
                    if msg.get("method") == "initialize":
                        sid = uuid.uuid4().hex
                        outer.session_ids.add(sid)
                        headers["Mcp-Session-Id"] = sid
                    elif self.headers.get("Mcp-Session-Id") not in outer.session_ids:
                        return self._json(400, {"error": "missing session"})
                    reply = handle_rpc(msg)
                    if reply is None:
                        self.send_response(202)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if outer.sse_replies and msg.get("method") == "tools/call":
                        body = (": keepalive\n\nevent: message\ndata: " + json.dumps(
                            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
                            + "\n\nevent: message\ndata: " + json.dumps(reply) + "\n\n").encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Content-Length", str(len(body)))
                        for k, v in headers.items():
                            self.send_header(k, v)
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    return self._json(200, reply, headers)
                self._json(405 if u.path == "/sse" else 404, {"error": "no"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return self.base + ("/sse" if self.mode == "sse" else "/mcp")

    def close(self):
        for q in self.sse_queues.values():
            q.put(None)
        self.httpd.shutdown()
        self.httpd.server_close()
