"""The test-job result: written by mode: test, checked strictly by mode: review."""

import json

import pytest

from dep_reviewer.config import Config, ConfigError
from dep_reviewer.gate import Checkout
from dep_reviewer.prompts import CheckRun
from dep_reviewer.test_result import (
    LOG_FILE,
    RESULT_FILE,
    TestResultError,
    checkout_for_review,
    clean_command,
    load_test_result,
    write_test_result,
)

HEAD = "a" * 40
MERGE = "b" * 40
BASE = "c" * 40


def written(tmp_path, exit_code=0, timed_out=False, command="npm ci && npm test"):
    (tmp_path / LOG_FILE).write_text("line 1\nline 2\nok\n")
    write_test_result(tmp_path, pr_number=7, head_sha=HEAD, checkout=Checkout(MERGE, True, "merge", BASE),
                      test=CheckRun(command, exit_code, timed_out, 12.34, 3, ""))
    return tmp_path


def load(tmp_path, head=HEAD, number=7):
    return load_test_result(tmp_path, pr_number=number, head_sha=head, log_tail_lines=2)


def tamper(tmp_path, **changes):
    path = tmp_path / RESULT_FILE
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


def test_round_trip(tmp_path):
    r = load(written(tmp_path))
    assert r.check.passed and r.check.command == "npm ci && npm test"
    assert r.check.log_line_count == 3 and r.check.log_tail == "line 2\nok"
    assert r.checkout_sha == MERGE and r.log_path == tmp_path / LOG_FILE


def test_failure_and_timeout_round_trip(tmp_path):
    assert load(written(tmp_path, exit_code=2)).check.exit_code == 2
    r = load(written(tmp_path, exit_code=None, timed_out=True))
    assert r.check.timed_out and not r.check.passed


def test_missing_result(tmp_path):
    with pytest.raises(TestResultError, match="not found"):
        load(tmp_path)


def test_result_for_another_head_is_rejected(tmp_path):
    with pytest.raises(TestResultError, match="not the reviewed head"):
        load(written(tmp_path), head="d" * 40)


def test_result_for_another_pr_is_rejected(tmp_path):
    with pytest.raises(TestResultError, match="another pull request"):
        load(written(tmp_path), number=8)


@pytest.mark.parametrize("changes", [
    {"exit_code": "0"},
    {"exit_code": True},
    {"timed_out": "false"},
    {"duration_s": -1},
    {"duration_s": True},
    {"head_sha": "HEAD"},
    {"checkout_sha": "x" * 40},
    {"version": 2},
    {"exit_code": None},  # disagrees with timed_out=false
    {"extra": 1},
])
def test_forged_shapes_are_rejected(tmp_path, changes):
    written(tmp_path)
    tamper(tmp_path, **changes)
    with pytest.raises(TestResultError):
        load(tmp_path)


def test_non_object_and_bad_json(tmp_path):
    written(tmp_path)
    (tmp_path / RESULT_FILE).write_text("[]")
    with pytest.raises(TestResultError):
        load(tmp_path)
    (tmp_path / RESULT_FILE).write_text("{")
    with pytest.raises(TestResultError):
        load(tmp_path)


def test_symlinked_log_is_rejected(tmp_path):
    written(tmp_path)
    (tmp_path / LOG_FILE).unlink()
    (tmp_path / LOG_FILE).symlink_to("/etc/hostname")
    with pytest.raises(TestResultError, match="test.log"):
        load(tmp_path)


def test_command_is_one_plain_line():
    assert clean_command("npm test`\n@here ```x") == "npm test  @here    x"
    assert len(clean_command("x" * 1000)) == 301


def test_same_merge_commit_keeps_review_checkout():
    review = Checkout(MERGE, True, "merge", BASE)
    assert checkout_for_review(review, MERGE) is review


def test_moved_base_between_jobs_is_not_a_merge_result():
    c = checkout_for_review(Checkout(MERGE, True, "merge", BASE), "e" * 40)
    assert not c.is_merge_result and "eeeeeee" in c.description and "bbbbbbb" in c.description


# --- mode rules in Config -------------------------------------------------

ENV = {"GITHUB_REPOSITORY": "o/r", "INPUT_GITHUB_TOKEN": "ghs"}


def test_test_mode_refuses_credentials():
    for key in ("INPUT_ANTHROPIC_API_KEY", "INPUT_CLAUDE_CODE_OAUTH_TOKEN"):
        with pytest.raises(ConfigError, match="must not receive"):
            Config.from_env({**ENV, "INPUT_MODE": "test", "INPUT_TEST_COMMAND": "t", key: "secret"})


def test_test_mode_needs_no_token_but_needs_a_command():
    cfg = Config.from_env({"GITHUB_REPOSITORY": "o/r", "INPUT_MODE": "test", "INPUT_TEST_COMMAND": "npm test"})
    assert cfg.mode == "test"
    with pytest.raises(ConfigError, match="needs test-command"):
        Config.from_env({"GITHUB_REPOSITORY": "o/r", "INPUT_MODE": "test"})


def test_review_mode_refuses_test_command():
    with pytest.raises(ConfigError, match="never runs the PR's code"):
        Config.from_env({**ENV, "INPUT_MODE": "review", "INPUT_CLAUDE_CODE_OAUTH_TOKEN": "t", "INPUT_TEST_COMMAND": "x"})


def test_oauth_token_and_test_command_cannot_share_a_job():
    with pytest.raises(ConfigError, match="can't share a job"):
        Config.from_env({**ENV, "INPUT_CLAUDE_CODE_OAUTH_TOKEN": "t", "INPUT_TEST_COMMAND": "x"})


def test_api_key_in_all_mode_still_allowed():
    cfg = Config.from_env({**ENV, "INPUT_ANTHROPIC_API_KEY": "k", "INPUT_TEST_COMMAND": "x"})
    assert cfg.mode == "all"


def test_unknown_mode():
    with pytest.raises(ConfigError, match="mode must be"):
        Config.from_env({**ENV, "INPUT_ANTHROPIC_API_KEY": "k", "INPUT_MODE": "both"})
