"""Serves the read-only repository tools to Claude Code over MCP (stdio, JSON-RPC 2.0).

Used by the Claude Code engine, so Claude sees exactly the tools the API engine gives
it: the same names, schemas, paging and confinement (read-only, inside the checkout,
never .git). Standard library only; one JSON message per line.

    python -P -m dep_reviewer.mcp_server --root DIR --base SHA --head SHA [--test-log FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .repo_tools import TOOL_DEFINITIONS, RepoTools, ToolError

SERVER_NAME = "repo"
DEFAULT_PROTOCOL = "2025-06-18"


def handle(tools: RepoTools, msg: dict[str, Any]) -> dict[str, Any] | None:
    """Answer one JSON-RPC message. Returns None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id")
    if msg_id is None:
        return None  # notification (notifications/initialized, cancellations): nothing to answer

    def ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        return ok({
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["input_schema"],
                "annotations": {"readOnlyHint": True},
            }
            for t in TOOL_DEFINITIONS
        ]})
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            text = tools.execute(params.get("name", ""), params.get("arguments") or {})
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except ToolError as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(tools: RepoTools, stdin=sys.stdin, stdout=sys.stdout) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply: dict[str, Any] | None = {
                "jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"},
            }
        else:
            reply = handle(tools, msg) if isinstance(msg, dict) else None
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dep_reviewer.mcp_server")
    p.add_argument("--root", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--test-log")
    a = p.parse_args(argv)
    tools = RepoTools(Path(a.root), a.base, a.head, Path(a.test_log) if a.test_log else None)
    serve(tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
