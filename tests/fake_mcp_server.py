"""A minimal, real MCP server over stdio, for tests. No dependency on the mcp SDK: just enough
JSON-RPC to exercise the client's handshake, tools/list and tools/call handling.

Usage: python fake_mcp_server.py [--fail-tool NAME] [--crash-on-call]
"""
import json
import sys

FAIL_TOOL = "--fail-tool" in sys.argv and sys.argv[sys.argv.index("--fail-tool") + 1]
CRASH_ON_CALL = "--crash-on-call" in sys.argv

TOOLS = [
    {"name": "echo", "description": "Echo the given text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "boom", "description": "Always returns an error result.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        req_id = msg.get("id")

        if method == "initialize":
            send({"jsonrpc": "2.0", "id": req_id,
                 "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake", "version": "1"},
                            "capabilities": {}}})
        elif method == "notifications/initialized":
            continue  # a notification: no response
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            if CRASH_ON_CALL:
                sys.exit(1)
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == FAIL_TOOL or name == "boom":
                send({"jsonrpc": "2.0", "id": req_id,
                     "result": {"content": [{"type": "text", "text": "tool failed on purpose"}], "isError": True}})
            elif name == "echo":
                send({"jsonrpc": "2.0", "id": req_id,
                     "result": {"content": [{"type": "text", "text": args.get("text", "")}]}})
            else:
                send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"unknown tool {name}"}})
        elif req_id is not None:
            send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"unknown method {method}"}})


if __name__ == "__main__":
    main()
