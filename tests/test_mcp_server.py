"""MCP server tests: the repository tools over JSON-RPC, as Claude Code sees them."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

from dep_reviewer.mcp_server import handle, serve
from dep_reviewer.repo_tools import TOOL_DEFINITIONS, RepoTools

SRC = Path(__file__).resolve().parent.parent / "src"


def make_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "app.js").write_text("const joi = require('joi')\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    return RepoTools(root, "a" * 40, "b" * 40)


def call(tools, method, params=None, msg_id=1):
    return handle(tools, {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})


def test_initialize_echoes_protocol(tmp_path):
    r = call(make_repo(tmp_path), "initialize", {"protocolVersion": "2025-03-26"})
    assert r["result"]["protocolVersion"] == "2025-03-26"
    assert "tools" in r["result"]["capabilities"]


def test_lists_same_tools_as_api_engine(tmp_path):
    listed = call(make_repo(tmp_path), "tools/list")["result"]["tools"]
    assert [t["name"] for t in listed] == [t["name"] for t in TOOL_DEFINITIONS]
    assert all(t["inputSchema"] == d["input_schema"] for t, d in zip(listed, TOOL_DEFINITIONS))


def test_call_runs_tool(tmp_path):
    r = call(make_repo(tmp_path), "tools/call", {"name": "search_code", "arguments": {"pattern": "joi"}})
    assert r["result"]["isError"] is False
    assert "app.js:1:" in r["result"]["content"][0]["text"]


def test_confinement_errors_are_tool_errors(tmp_path):
    tools = make_repo(tmp_path)
    for path in ("../outside", ".git/config"):
        r = call(tools, "tools/call", {"name": "read_file", "arguments": {"path": path}})
        assert r["result"]["isError"] is True


def test_notifications_get_no_reply(tmp_path):
    assert handle(make_repo(tmp_path), {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method(tmp_path):
    assert call(make_repo(tmp_path), "resources/list")["error"]["code"] == -32601


def test_serve_line_protocol(tmp_path):
    tools = make_repo(tmp_path)
    stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n')
    stdout = io.StringIO()
    serve(tools, stdin, stdout)
    replies = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert replies[1]["error"]["code"] == -32700


def test_runs_as_module_from_inside_the_repo(tmp_path):
    """Started the way the MCP config starts it: -P, cwd in the reviewed repo, which
    plants a module that would shadow ours if the cwd were on sys.path."""
    tools = make_repo(tmp_path)
    (tools.root / "dep_reviewer").mkdir()
    (tools.root / "dep_reviewer" / "__init__.py").write_text("raise SystemExit('shadowed')\n")
    proc = subprocess.run(
        [sys.executable, "-P", "-m", "dep_reviewer.mcp_server", "--root", str(tools.root),
         "--base", "a" * 40, "--head", "b" * 40],
        input='{"jsonrpc":"2.0","id":7,"method":"tools/list"}\n', capture_output=True, text=True,
        cwd=tools.root, env={**os.environ, "PYTHONPATH": str(SRC)}, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["id"] == 7
