"""Claude Code engine: the same review, run through the `claude` CLI instead of the API.

Lets teams that authenticate with a Claude Code OAuth token (`claude setup-token`) use
the gate without an API key. Everything around the review is unchanged: the same system
prompt, PR context message, verdict schema and read-only repository tools, which reach
Claude Code through the MCP server in mcp_server.py. Its output is a ReviewOutcome, so
the gate, report and auto-merge rules see no difference between the engines.

Isolation. Claude Code normally loads CLAUDE.md, settings and hooks from its working
directory, and a hook runs a shell command. Run inside the PR's checkout, that would let
the reviewed code execute with the OAuth token in its environment. So `claude` runs in a
private directory outside the checkout, with project and user settings off, a private
config directory, no built-in tools except web search and fetch, and only the MCP server
given here. The environment is rebuilt from scratch: no GitHub token, no INPUT_* values.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .claude_review import FINISH_NOW, GRACE_TURNS, ReviewError, ReviewOutcome, parse_review
from .mcp_server import SERVER_NAME
from .repo_tools import TOOL_DEFINITIONS, RepoTools
from .schema import REVIEW_SCHEMA

# Built-in Claude Code tools the review may use: the CLI's counterparts of the API
# engine's web_search and web_fetch server tools. Nothing that runs code or writes files.
WEB_TOOLS = ("WebSearch", "WebFetch")
# Upper bound for one `claude` invocation, so a stuck process cannot hold the job.
CLI_TIMEOUT_SECONDS = 30 * 60
# Environment variables passed through to `claude`. Everything else is dropped.
PASSTHROUGH_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")

TOOL_NAMING_NOTE = (
    "\n\nTool names in this session: the repository tools are prefixed "
    f"`mcp__{SERVER_NAME}__` (for example `mcp__{SERVER_NAME}__search_code` is search_code), "
    "and web search and fetch are called WebSearch and WebFetch."
)


def mcp_tool_names() -> list[str]:
    return [f"mcp__{SERVER_NAME}__{t['name']}" for t in TOOL_DEFINITIONS]


def write_mcp_config(work_dir: Path, tools: RepoTools) -> Path:
    """MCP config that starts the repository tool server with this interpreter and package."""
    src_dir = Path(__file__).resolve().parent.parent
    args = ["-P", "-m", "dep_reviewer.mcp_server",
            "--root", str(tools.root), "--base", tools.base_sha, "--head", tools.head_sha]
    if tools.test_log is not None:
        args += ["--test-log", str(tools.test_log)]
    config = {"mcpServers": {SERVER_NAME: {
        "type": "stdio",
        "command": sys.executable,
        "args": args,
        "env": {"PYTHONPATH": str(src_dir)},
    }}}
    path = work_dir / "mcp.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def cli_env(oauth_token: str, config_dir: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if base is None else base
    env = {k: source[k] for k in PASSTHROUGH_ENV if k in source}
    env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def base_command(claude_bin: str, *, model: str, effort: str, system: str) -> list[str]:
    return [
        claude_bin, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(REVIEW_SCHEMA),
        "--model", model,
        "--effort", effort,
        "--system-prompt", system + TOOL_NAMING_NOTE,
        "--restricted",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
    ]


def allowed_tools(web_fetch_domains: tuple[str, ...]) -> list[str]:
    """The repository tools, WebSearch, and WebFetch only for the listed domains.

    In print mode a tool call that matches no allow rule is denied, so WebFetch to any
    other host fails (it shows up in the result's permission_denials)."""
    return [*mcp_tool_names(), "WebSearch", *(f"WebFetch(domain:{d})" for d in web_fetch_domains)]


def investigate_command(base: list[str], mcp_config: Path, max_turns: int,
                        web_fetch_domains: tuple[str, ...]) -> list[str]:
    return [
        *base,
        "--mcp-config", str(mcp_config),
        "--tools", ",".join(WEB_TOOLS),
        "--allowedTools", ",".join(allowed_tools(web_fetch_domains)),
        "--max-turns", str(max_turns),
    ]


def finish_command(base: list[str], session_id: str) -> list[str]:
    """Resume the session with no tools at all, so Claude can only answer."""
    return [*base, "--resume", session_id, "--tools", "", "--max-turns", str(GRACE_TURNS)]


def run_cli(cmd: list[str], prompt: str, *, cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, input=prompt, cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=CLI_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise ReviewError(f"Claude Code did not finish within {CLI_TIMEOUT_SECONDS // 60} minutes") from None
    except FileNotFoundError:
        raise ReviewError(f"Claude Code CLI not found at {cmd[0]}") from None
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # stderr can echo request details; report only that the run failed.
        raise ReviewError(f"Claude Code exited with {proc.returncode} without a JSON result") from None
    if not isinstance(result, dict):
        raise ReviewError("Claude Code returned an unexpected result")
    return result


def add_usage(total: dict[str, int], result: dict[str, Any]) -> None:
    usage = result.get("usage") or {}
    for key in USAGE_KEYS:
        total[key] = total.get(key, 0) + int(usage.get(key) or 0)


def review_from(result: dict[str, Any]) -> dict | None:
    """The structured verdict, or None when the run ended without one."""
    if result.get("subtype") != "success" or result.get("is_error"):
        return None
    structured = result.get("structured_output")
    if structured is None:
        return None
    return parse_review(json.dumps(structured))


def refused(turns: int, usage: dict[str, int]) -> ReviewOutcome:
    return ReviewOutcome(review=None, unavailable_reason="Claude declined to review this PR.",
                         turns=turns, usage=usage)


def describe_failure(result: dict[str, Any]) -> str:
    """Why a run gave no verdict, from the result's fixed fields only (never its free text)."""
    status = result.get("api_error_status")
    if status in (401, 403):
        return f"Claude Code could not authenticate (HTTP {status}); check claude-code-oauth-token"
    if status:
        return f"the Claude API returned HTTP {status} to Claude Code"
    reason = result.get("terminal_reason") or result.get("subtype") or "unknown"
    return f"Claude Code ended without a verdict ({reason})"


def run_review_cli(
    *,
    oauth_token: str,
    model: str,
    effort: str,
    max_turns: int,
    system: str,
    user_message: str,
    tools: RepoTools,
    work_dir: Path,
    web_fetch_domains: tuple[str, ...],
    claude_bin: str = "claude",
    log=print,
) -> ReviewOutcome:
    work_dir.mkdir(parents=True, exist_ok=True)
    config_dir = work_dir / "claude-config"
    config_dir.mkdir(exist_ok=True)
    env = cli_env(oauth_token, config_dir)
    base = base_command(claude_bin, model=model, effort=effort, system=system)
    usage: dict[str, int] = {}

    result = run_cli(investigate_command(base, write_mcp_config(work_dir, tools), max_turns, web_fetch_domains),
                     user_message, cwd=work_dir, env=env)
    for denial in result.get("permission_denials") or []:
        if isinstance(denial, dict):
            log(f"  denied: {denial.get('tool_name')} (not in the allowed tools or web-fetch-domains)")
    add_usage(usage, result)
    turns = int(result.get("num_turns") or 0)

    if result.get("stop_reason") == "refusal":
        return refused(turns, usage)

    review = review_from(result)
    if review is None and result.get("subtype") == "error_max_turns" and result.get("session_id"):
        log(f"  reached max-turns ({max_turns}); asking Claude for its verdict")
        result = run_cli(finish_command(base, result["session_id"]), FINISH_NOW, cwd=work_dir, env=env)
        add_usage(usage, result)
        turns += int(result.get("num_turns") or 0)
        if result.get("stop_reason") == "refusal":
            return refused(turns, usage)
        review = review_from(result)
        if review is None:
            raise ReviewError("Claude did not give a verdict after being asked to finish")

    if review is None:
        raise ReviewError(describe_failure(result))
    log(f"Review finished in {turns} turns: risk={review['risk']} confidence={review['confidence']}")
    return ReviewOutcome(review=review, turns=turns, usage=usage)
