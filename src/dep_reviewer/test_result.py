"""Hands the build and test result from the test job to the review job.

With `mode: test` and `mode: review` the PR's code and the credentials never share a
job: anything running as the same user can read another process's environment through
/proc/<pid>/environ, so a credential held by the action while `test-command` runs is
readable by the dependency being tested. The test job writes `result.json` and
`test.log`; the review job downloads them and checks them before use.

Both files were written after the PR's code ran in that job, so they are untrusted:
the review job accepts only the exact shape below, ties the result to the PR head it is
reviewing, and treats anything else as "no tests were run" (a needs-human verdict).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .gate import Checkout
from .prompts import CheckRun

VERSION = 1
RESULT_FILE = "result.json"
LOG_FILE = "test.log"
MAX_RESULT_BYTES = 16 * 1024
MAX_LOG_BYTES = 50 * 1024 * 1024
MAX_COMMAND_CHARS = 300
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class TestResultError(Exception):
    """The test job's result is missing, malformed or for another commit."""

    __test__ = False  # not a pytest test class


@dataclass(frozen=True)
class TestResult:
    __test__ = False

    check: CheckRun
    checkout_sha: str
    log_path: Path


def write_test_result(out_dir: Path, *, pr_number: int, head_sha: str, checkout: Checkout, test: CheckRun) -> None:
    """Called by the test job after test-command; test.log is already in out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / RESULT_FILE).write_text(json.dumps({
        "version": VERSION,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "checkout_sha": checkout.sha,
        "command": test.command,
        "exit_code": test.exit_code,
        "timed_out": test.timed_out,
        "duration_s": round(test.duration_s, 1),
    }), encoding="utf-8")


def _field(data: dict, key: str, kind: type | tuple[type, ...]):
    value = data.get(key)
    # bool is an int subclass: never accept one where a number is expected.
    if isinstance(value, bool) and kind is not bool:
        raise TestResultError(f"result.json: '{key}' has the wrong type")
    if not isinstance(value, kind):
        raise TestResultError(f"result.json: '{key}' has the wrong type")
    return value


def clean_command(command: str) -> str:
    """The command is shown in the PR comment inside backticks; keep it to one plain line."""
    flat = re.sub(r"[\x00-\x1f\x7f`]", " ", command).strip()
    return flat if len(flat) <= MAX_COMMAND_CHARS else flat[:MAX_COMMAND_CHARS] + "…"


def load_test_result(result_dir: Path, *, pr_number: int, head_sha: str, log_tail_lines: int) -> TestResult:
    path = result_dir / RESULT_FILE
    log_path = result_dir / LOG_FILE
    if not path.is_file() or path.is_symlink():
        raise TestResultError("the test job's result.json was not found")
    if path.stat().st_size > MAX_RESULT_BYTES:
        raise TestResultError("result.json is too large")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TestResultError("result.json is not valid JSON") from None
    if not isinstance(data, dict):
        raise TestResultError("result.json is not an object")
    expected = {"version", "pr_number", "head_sha", "checkout_sha", "command", "exit_code", "timed_out", "duration_s"}
    if set(data) != expected:
        raise TestResultError("result.json does not have the expected fields")
    if _field(data, "version", int) != VERSION:
        raise TestResultError("result.json has an unsupported version")
    if _field(data, "pr_number", int) != pr_number:
        raise TestResultError("result.json is for another pull request")
    result_head = _field(data, "head_sha", str)
    checkout_sha = _field(data, "checkout_sha", str)
    if not SHA_RE.match(result_head) or not SHA_RE.match(checkout_sha):
        raise TestResultError("result.json has an invalid commit SHA")
    if result_head != head_sha:
        raise TestResultError(f"the tests ran on PR head {result_head[:7]}, not the reviewed head {head_sha[:7]}")
    timed_out = _field(data, "timed_out", bool)
    exit_code = data.get("exit_code")
    if exit_code is not None and (not isinstance(exit_code, int) or isinstance(exit_code, bool)):
        raise TestResultError("result.json: 'exit_code' has the wrong type")
    if (exit_code is None) != timed_out:
        raise TestResultError("result.json: exit_code and timed_out disagree")
    duration = _field(data, "duration_s", (int, float))
    if duration < 0:
        raise TestResultError("result.json: 'duration_s' is negative")

    if not log_path.is_file() or log_path.is_symlink():
        raise TestResultError("the test job's test.log was not found")
    if log_path.stat().st_size > MAX_LOG_BYTES:
        raise TestResultError("test.log is too large")
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    check = CheckRun(
        command=clean_command(_field(data, "command", str)),
        exit_code=exit_code,
        timed_out=timed_out,
        duration_s=float(duration),
        log_line_count=len(lines),
        log_tail="\n".join(lines[-log_tail_lines:]),
    )
    return TestResult(check=check, checkout_sha=checkout_sha, log_path=log_path)


def checkout_for_review(review: Checkout, tested_sha: str) -> Checkout:
    """The review job's checkout, unless the test job tested a different commit.

    Each job checks out refs/pull/N/merge, which GitHub recomputes when the base branch
    moves, so the two jobs can see different merge commits. Then the tests don't prove
    what the review saw or what will land, and the verdict must not be a pass."""
    if tested_sha == review.sha:
        return review
    return Checkout(
        review.sha, False,
        f"commit {tested_sha[:7]} in the test job, but the review checked out {review.sha[:7]} "
        "(the base branch moved between the jobs)",
    )
