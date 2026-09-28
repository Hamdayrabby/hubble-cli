"""Tiny local stand-in for the Anthropic Messages API, for tests: GET /v1/models and streaming
POST /v1/messages. Each POST pops the next scripted reply; every request body is recorded."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def text_reply(text, stop="end_turn"):
    return {"blocks": [{"type": "text", "text": text}], "stop": stop}


def tool_reply(tool_id, name, args, text="", thinking=None):
    blocks = []
    if thinking is not None:
        blocks.append({"type": "thinking", "thinking": thinking, "signature": "sig-" + tool_id})
    if text:
        blocks.append({"type": "text", "text": text})
    blocks.append({"type": "tool_use", "id": tool_id, "name": name, "input": args})
    return {"blocks": blocks, "stop": "tool_use"}


class FakeAnthropic:
    def __init__(self, replies, models=("claude-opus-5", "claude-haiku-4-5"), status=200):
        self.replies = list(replies)
        self.requests = []
        self.headers = []
        self.status = status
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                outer.headers.append(dict(self.headers))
                if self.path.startswith("/v1/models"):
                    data = [{"type": "model", "id": m, "display_name": m, "created_at": "2026-01-01T00:00:00Z",
                             "max_input_tokens": 1000000 if "opus" in m else 200000, "max_tokens": 128000}
                            for m in models]
                    return self._send(200, json.dumps({"data": data, "has_more": False,
                                                       "first_id": models[0], "last_id": models[-1]}))
                self._send(404, "{}")

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                outer.requests.append(body)
                outer.headers.append(dict(self.headers))
                if outer.status != 200:
                    return self._send(outer.status, json.dumps(
                        {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
                reply = outer.replies.pop(0)
                self._send(200, self._sse(body["model"], reply), "text/event-stream")

            def _sse(self, model, reply):
                out = []

                def ev(kind, data):
                    out.append(f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n")

                ev("message_start", {"message": {"id": "msg_1", "type": "message", "role": "assistant",
                                                 "model": model, "content": [], "stop_reason": None,
                                                 "stop_sequence": None,
                                                 "usage": {"input_tokens": 100, "output_tokens": 1,
                                                           "cache_read_input_tokens": 50,
                                                           "cache_creation_input_tokens": 0}}})
                for i, b in enumerate(reply["blocks"]):
                    if b["type"] == "text":
                        ev("content_block_start", {"index": i, "content_block": {"type": "text", "text": ""}})
                        for chunk in (b["text"][:3], b["text"][3:]):
                            if chunk:
                                ev("content_block_delta", {"index": i, "delta": {"type": "text_delta", "text": chunk}})
                    elif b["type"] == "thinking":
                        ev("content_block_start", {"index": i, "content_block": {"type": "thinking", "thinking": "",
                                                                                  "signature": ""}})
                        if b["thinking"]:
                            ev("content_block_delta", {"index": i, "delta": {"type": "thinking_delta",
                                                                             "thinking": b["thinking"]}})
                        ev("content_block_delta", {"index": i, "delta": {"type": "signature_delta",
                                                                         "signature": b["signature"]}})
                    elif b["type"] == "tool_use":
                        ev("content_block_start", {"index": i, "content_block": {
                            "type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}})
                        raw = json.dumps(b["input"])
                        for chunk in (raw[:5], raw[5:]):
                            ev("content_block_delta", {"index": i, "delta": {"type": "input_json_delta",
                                                                             "partial_json": chunk}})
                    ev("content_block_stop", {"index": i})
                ev("message_delta", {"delta": {"stop_reason": reply["stop"], "stop_sequence": None},
                                     "usage": {"output_tokens": 42}})
                ev("message_stop", {})
                return "".join(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
