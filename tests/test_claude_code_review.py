"""Claude Code engine tests with a fake `claude` executable (no network, no CLI install)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dep_reviewer.claude_code_review import WEB_TOOLS, mcp_tool_names, run_review_cli
from dep_reviewer.claude_review import FINISH_NOW, ReviewError
from dep_reviewer.repo_tools import RepoTools
from tests.test_report import REVIEW

FAKE_CLAUDE = """#!{python}
import json, os, sys
from pathlib import Path
here = Path({here!r})
calls = json.loads((here / "calls.json").read_text()) if (here / "calls.json").exists() else []
replies = json.loads((here / "replies.json").read_text())
calls.append({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(), "env": dict(os.environ)}})
(here / "calls.json").write_text(json.dumps(calls))
reply = replies[len(calls) - 1]
if isinstance(reply, str):
    print(reply)
else:
    print(json.dumps(reply))
"""


def success(review=REVIEW, turns=4):
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": turns,
            "session_id": "s1", "structured_output": review,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}


def max_turns_hit(turns=3):
    return {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": turns,
            "session_id": "s1", "usage": {"input_tokens": 7, "output_tokens": 1}}


@pytest.fixture
def fake(tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable, here=str(bin_dir)))
    script.chmod(0o755)

    class Fake:
        path = script

        def replies(self, *items):
            (bin_dir / "replies.json").write_text(json.dumps(list(items)))

        @property
        def calls(self):
            return json.loads((bin_dir / "calls.json").read_text())

    return Fake()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return RepoTools(root, "a" * 40, "b" * 40)


def run(fake, repo, tmp_path, max_turns=10):
    return run_review_cli(oauth_token="sk-ant-oat-test", model="m", effort="high", max_turns=max_turns,
                          system="SYSTEM", user_message="USER", tools=repo,
                          work_dir=tmp_path / "work", web_fetch_domains=("github.com", "pypi.org"),
                          claude_bin=str(fake.path), log=lambda *_: None)


def flag(argv, name):
    return argv[argv.index(name) + 1]


def test_verdict_and_usage(fake, repo, tmp_path):
    fake.replies(success())
    out = run(fake, repo, tmp_path)
    assert out.risk == "medium" and out.turns == 4
    assert out.usage == {"input_tokens": 10, "output_tokens": 5,
                         "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}
    call = fake.calls[0]
    assert call["stdin"] == "USER"
    assert flag(call["argv"], "--system-prompt").startswith("SYSTEM")
    assert json.loads(flag(call["argv"], "--json-schema"))["required"]


def test_isolation_flags_and_environment(fake, repo, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    monkeypatch.setenv("INPUT_GITHUB_TOKEN", "ghs_secret")
    fake.replies(success())
    run(fake, repo, tmp_path)
    call = fake.calls[0]
    argv = call["argv"]

    # Runs outside the checkout, so its CLAUDE.md and .claude/ settings are never loaded.
    assert Path(call["cwd"]).resolve() == (tmp_path / "work").resolve()
    assert not Path(call["cwd"]).resolve().is_relative_to(repo.root)
    for f in ("--restricted", "--strict-mcp-config", "--disable-slash-commands"):
        assert f in argv
    assert flag(argv, "--setting-sources") == ""
    # Only web tools are built in; the repository tools come from our MCP server.
    assert flag(argv, "--tools") == ",".join(WEB_TOOLS)
    # WebFetch only to the listed domains: a bare "WebFetch" would allow any URL.
    assert set(flag(argv, "--allowedTools").split(",")) == {
        *mcp_tool_names(), "WebSearch", "WebFetch(domain:github.com)", "WebFetch(domain:pypi.org)"}
    assert flag(argv, "--max-turns") == "10"

    env = call["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test"
    assert Path(env["CLAUDE_CONFIG_DIR"]).resolve().is_relative_to((tmp_path / "work").resolve())
    assert "GITHUB_TOKEN" not in env and not any(k.startswith("INPUT_") for k in env)


def test_mcp_config_points_at_checkout(fake, repo, tmp_path):
    fake.replies(success())
    run(fake, repo, tmp_path)
    config = json.loads(Path(flag(fake.calls[0]["argv"], "--mcp-config")).read_text())
    server = config["mcpServers"]["repo"]
    assert server["command"] == sys.executable
    assert server["args"][:3] == ["-P", "-m", "dep_reviewer.mcp_server"]
    assert flag(server["args"], "--root") == str(repo.root)
    assert flag(server["args"], "--head") == "b" * 40


def test_turn_limit_resumes_without_tools(fake, repo, tmp_path):
    fake.replies(max_turns_hit(), success(turns=1))
    out = run(fake, repo, tmp_path, max_turns=3)
    assert out.risk == "medium" and out.turns == 4
    assert out.usage["input_tokens"] == 17
    second = fake.calls[1]
    assert flag(second["argv"], "--resume") == "s1"
    assert flag(second["argv"], "--tools") == ""
    assert "--mcp-config" not in second["argv"]
    assert second["stdin"] == FINISH_NOW


def test_no_verdict_after_finish_raises(fake, repo, tmp_path):
    fake.replies(max_turns_hit(), max_turns_hit())
    with pytest.raises(ReviewError, match="after being asked to finish"):
        run(fake, repo, tmp_path)


def test_refusal_gives_unknown(fake, repo, tmp_path):
    fake.replies({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                  "stop_reason": "refusal", "usage": {}})
    out = run(fake, repo, tmp_path)
    assert out.risk == "unknown" and "declined" in out.unavailable_reason


def test_refusal_in_finish_call_gives_unknown(fake, repo, tmp_path):
    fake.replies(max_turns_hit(), {"type": "result", "subtype": "success", "is_error": False,
                                   "num_turns": 1, "stop_reason": "refusal", "usage": {}})
    out = run(fake, repo, tmp_path)
    assert out.risk == "unknown" and "declined" in out.unavailable_reason


def test_error_result_raises(fake, repo, tmp_path):
    fake.replies({"type": "result", "subtype": "error_during_execution", "is_error": True, "num_turns": 1})
    with pytest.raises(ReviewError, match="error_during_execution"):
        run(fake, repo, tmp_path)


def test_auth_failure_is_named(fake, repo, tmp_path):
    # Shape returned by Claude Code 2.1.319 for a bad token: subtype is still "success".
    fake.replies({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
                  "api_error_status": 401, "terminal_reason": "api_error",
                  "result": "Failed to authenticate. API Error: 401 OAuth access token is invalid."})
    with pytest.raises(ReviewError, match="could not authenticate"):
        run(fake, repo, tmp_path)


def test_non_json_output_raises_without_echoing_it(fake, repo, tmp_path):
    fake.replies("Invalid OAuth token sk-ant-oat-test")
    with pytest.raises(ReviewError) as e:
        run(fake, repo, tmp_path)
    assert "sk-ant-oat" not in str(e.value)


def test_missing_required_field_raises(fake, repo, tmp_path):
    fake.replies(success(review={"risk": "low"}))
    with pytest.raises(ReviewError, match="missing"):
        run(fake, repo, tmp_path)
