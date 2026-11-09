"""mode: test then mode: review, end to end on a real git checkout, with GitHub and Claude faked."""

import copy
import json
import subprocess

import pytest

from dep_reviewer import __main__ as main
from dep_reviewer.claude_review import ReviewOutcome
from dep_reviewer.config import Config
from dep_reviewer.test_result import RESULT_FILE
from tests.test_report import REVIEW

PASSING_REVIEW = copy.deepcopy(REVIEW)
PASSING_REVIEW.update(risk="low", confidence="high")
for item in PASSING_REVIEW["impacted_code"]:
    item["covered_by_tests"] = "yes"


def git(root, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                          cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    """A workspace like actions/checkout on pull_request: the PR merged into its base."""
    root = tmp_path / "ws"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "package.json").write_text('{"dependencies": {"left-pad": "1.0.0"}}\n')
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    base = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-qb", "dependabot")
    (root / "package.json").write_text('{"dependencies": {"left-pad": "2.0.0"}}\n')
    git(root, "commit", "-qam", "bump")
    head = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "--detach", base)
    git(root, "merge", "--no-ff", "-qm", "merge", head)
    return root, base, head


class FakeGitHub:
    def __init__(self, pr):
        self.pr = pr
        self.comment = None

    def get_pr(self, number):
        return self.pr

    def list_pr_files(self, number):
        return []

    def upsert_comment(self, number, body, marker):
        self.comment = body
        return "comment-url"

    def ensure_label(self, *a):
        pass

    def set_exclusive_label(self, *a):
        pass


def config(tmp_path, root, **inputs):
    env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_WORKSPACE": str(root), "RUNNER_TEMP": str(tmp_path / "temp"),
           "GITHUB_EVENT_NAME": "pull_request"}
    env.update({f"INPUT_{k.upper()}": v for k, v in inputs.items()})
    return Config.from_env(env)


def outputs(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


@pytest.fixture
def run_both(tmp_path, checkout, monkeypatch):
    root, base, head = checkout
    out = tmp_path / "outputs"
    out.write_text("")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    pr = {"number": 7, "node_id": "N", "title": "Bump left-pad", "body": "", "html_url": "https://x/pr/7",
          "user": {"login": "dependabot[bot]"}, "labels": [], "head": {"sha": head},
          "base": {"sha": base, "ref": "main"}}
    event = {"pull_request": pr}

    def test_job(command="echo tested"):
        cfg = config(tmp_path, root, mode="test", test_command=command)
        assert main.run_test_job(cfg, event) == 0
        return cfg

    def review_job(tamper=None):
        if tamper:
            path = tmp_path / "temp" / "dep-review-test-result" / RESULT_FILE
            data = json.loads(path.read_text())
            data.update(tamper)
            path.write_text(json.dumps(data))
        cfg = config(tmp_path, root, mode="review", claude_code_oauth_token="tok", github_token="ghs")

        def no_tests(*a, **k):
            raise AssertionError("mode: review must never run test-command")

        monkeypatch.setattr(main, "run_checks", no_tests)
        monkeypatch.setattr(main, "run_review_cli", lambda **k: ReviewOutcome(review=PASSING_REVIEW))
        gh = FakeGitHub(pr)
        main.review_pr(cfg, gh, event)
        return outputs(out)["verdict"], gh.comment

    return test_job, review_job


def test_passing_tests_then_review_gives_pass(run_both):
    test_job, review_job = run_both
    test_job()
    verdict, comment = review_job()
    assert verdict == "pass"
    assert "echo tested" in comment


def test_failing_tests_give_fail(run_both):
    test_job, review_job = run_both
    test_job("exit 3")
    assert review_job()[0] == "fail"


def test_no_test_result_gives_needs_human(run_both):
    _, review_job = run_both
    assert review_job()[0] == "needs-human"


def test_tests_on_another_head_give_needs_human(run_both):
    test_job, review_job = run_both
    test_job()
    assert review_job(tamper={"head_sha": "d" * 40})[0] == "needs-human"


def test_base_moved_between_jobs_gives_needs_human(run_both):
    test_job, review_job = run_both
    test_job()
    verdict, comment = review_job(tamper={"checkout_sha": "e" * 40})
    assert verdict == "needs-human" and "moved between the jobs" in comment

