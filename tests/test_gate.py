import subprocess
from pathlib import Path

import pytest

from dep_reviewer.claude_review import ReviewOutcome
from dep_reviewer.config import should_fail
from dep_reviewer.gate import (
    FAIL,
    NEEDS_HUMAN,
    PASS,
    Checkout,
    decide,
    describe_checkout,
    parse_update_entries,
    pr_number_from_queue_ref,
)
from dep_reviewer.prompts import CheckRun
from dep_reviewer.report import RenderContext, render_comment
from tests.test_report import REVIEW

MERGE = Checkout("m" * 40, True, "merge result")
HEAD_ONLY = Checkout("h" * 40, False, "the PR's own commit")
PASSED = CheckRun("npm test", 0, False, 1.0, 10, "")
FAILED = CheckRun("npm test", 1, False, 1.0, 10, "")
TIMED_OUT = CheckRun("npm test", None, True, 1.0, 10, "")

CLEAN = dict(
    REVIEW,
    risk="low",
    confidence="high",
    impacted_code=[{"file": "a.js", "line": 1, "description": "x", "covered_by_tests": "yes"}],
)


def outcome(**overrides) -> ReviewOutcome:
    return ReviewOutcome(review=dict(CLEAN, **overrides))


def test_pass_needs_all_evidence():
    v = decide(outcome(), PASSED, MERGE)
    assert v.status == PASS


def test_pass_with_no_impacted_code():
    assert decide(outcome(impacted_code=[]), PASSED, MERGE).status == PASS


@pytest.mark.parametrize("test", [FAILED, TIMED_OUT])
def test_failing_tests_fail_even_if_claude_is_happy(test):
    v = decide(outcome(), test, MERGE)
    assert v.status == FAIL


def test_high_risk_fails():
    assert decide(outcome(risk="high"), PASSED, MERGE).status == FAIL


@pytest.mark.parametrize(
    "out, test, checkout",
    [
        (outcome(), None, MERGE),  # no tests
        (outcome(), PASSED, HEAD_ONLY),  # not the merge result
        (outcome(risk="medium"), PASSED, MERGE),
        (outcome(confidence="low"), PASSED, MERGE),
        (outcome(impacted_code=[{"file": "a", "line": 1, "description": "", "covered_by_tests": "no"}]), PASSED, MERGE),
        (outcome(impacted_code=[{"file": "a", "line": 1, "description": "", "covered_by_tests": "unknown"}]), PASSED, MERGE),
        (ReviewOutcome(review=None, unavailable_reason="API down"), PASSED, MERGE),
    ],
)
def test_needs_human(out, test, checkout):
    assert decide(out, test, checkout).status == NEEDS_HUMAN


def test_fail_lists_all_reasons():
    v = decide(outcome(risk="medium"), FAILED, HEAD_ONLY)
    assert v.status == FAIL
    assert len(v.reasons) == 3


@pytest.mark.parametrize(
    "verdict, fail_on, expected",
    [
        (FAIL, "fail", True),
        (NEEDS_HUMAN, "fail", False),
        (PASS, "fail", False),
        (NEEDS_HUMAN, "needs-human", True),
        (PASS, "needs-human", False),
        (FAIL, "never", False),
    ],
)
def test_should_fail(verdict, fail_on, expected):
    assert should_fail(verdict, fail_on) is expected


def test_queue_ref_parsing():
    assert pr_number_from_queue_ref("refs/heads/gh-readonly-queue/main/pr-123-0a1b2c3d4e5f") == 123
    assert pr_number_from_queue_ref("refs/heads/main") is None


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_describe_checkout_detects_merge_result(tmp_path: Path):
    git(tmp_path, "init", "-q", "-b", "main")
    for k, v in (("user.email", "t@e.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(tmp_path, "config", k, v)
    (tmp_path / "a").write_text("1")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "dependabot")
    (tmp_path / "b").write_text("2")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "bump")
    head = git(tmp_path, "rev-parse", "HEAD")

    assert describe_checkout(tmp_path, head, "main").is_merge_result is False

    git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "c").write_text("3")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "main moved on")
    git(tmp_path, "merge", "-q", "--no-ff", "-m", "merge", "dependabot")
    c = describe_checkout(tmp_path, head, "main")
    assert c.is_merge_result is True
    assert "merged into main" in c.description


def test_discarded_test_result_says_why():
    reason = "the test job's result was not used (the tests ran on PR head aaaaaaa, not the reviewed head bbbbbbb)"
    verdict = decide(outcome(**CLEAN), None, MERGE, reason)
    assert verdict.status == NEEDS_HUMAN
    assert verdict.reasons[0].startswith("The test job's result was not used")
    assert "set `test-command`" not in verdict.reasons[0]
    body = render_comment(outcome(**CLEAN), verdict, RenderContext("https://github.com", "o/r", "m", MERGE, None, None, reason))
    assert f"**Tested:** {reason}" in body


def test_metadata_entries_from_a_grouped_commit():
    message = """Bump the npm-minor group

---
updated-dependencies:
- dependency-name: a
  dependency-version: 1.0.0
  dependency-type: direct:production
  update-type: version-update:semver-minor
  dependency-group: npm-minor
- dependency-name: "@scope/b"
  dependency-version: 2.0.0
  dependency-type: direct:development
  update-type: version-update:semver-minor
  dependency-group: npm-minor
...
"""
    entries = parse_update_entries(message)
    assert [e["dependency-name"] for e in entries] == ["a", "@scope/b"]
    assert entries[1]["dependency-type"] == "direct:development"
