from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path

import anthropic

from .claude_review import ReviewError, ReviewOutcome, run_review
from .claude_code_review import run_review_cli
from .config import Config, ConfigError, should_fail
from .gate import (
    PASS,
    Checkout,
    Verdict,
    auto_merge_decision,
    decide,
    describe_checkout,
    parse_dependabot_metadata,
    pr_number_from_queue_ref,
)
from .github_api import GitHub, GitHubError
from .prompts import SYSTEM_PROMPT, CheckRun, build_user_message
from .repo_tools import RepoTools
from .report import COMMENT_MARKER, LABEL_COLORS, LABEL_DESCRIPTIONS, RenderContext, render_comment
from .test_result import LOG_FILE, TestResultError, checkout_for_review, load_test_result, write_test_result
from .tickets import build_content, render_ticket_section

LOG_TAIL_LINES = 150


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def append_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


def load_event(cfg: Config) -> dict:
    if cfg.event_path and cfg.event_path.exists():
        return json.loads(cfg.event_path.read_text(encoding="utf-8"))
    return {}


def run_checks(cfg: Config, log_path: Path) -> CheckRun:
    print(f"::group::Running test-command: {cfg.test_command}")
    start = time.monotonic()
    timed_out = False
    exit_code: int | None
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            ["bash", "-o", "pipefail", "-c", cfg.test_command],
            cwd=cfg.workspace,
            stdout=log,
            stderr=subprocess.STDOUT,
            # The test command does not need the Anthropic key or the GitHub token.
            env={k: v for k, v in os.environ.items() if not k.startswith("INPUT_")},
        )
        try:
            exit_code = proc.wait(timeout=cfg.test_timeout_minutes * 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            exit_code, timed_out = None, True
    duration = time.monotonic() - start
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines))
    print("::endgroup::")
    run = CheckRun(
        command=cfg.test_command,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_s=duration,
        log_line_count=len(lines),
        log_tail="\n".join(lines[-LOG_TAIL_LINES:]),
    )
    print(f"test-command {run.describe()}")
    return run


def run_merge_queue(cfg: Config, gh: GitHub, event: dict) -> int:
    """In a merge queue, re-run only the build and tests on the exact commit that will land.

    Claude already reviewed the PR when it was opened or updated, so it is not re-run here.
    """
    ref = (event.get("merge_group") or {}).get("head_ref", "")
    number = pr_number_from_queue_ref(ref)
    if number is not None:
        author = gh.get_pr(number)["user"]["login"].lower()
        if author not in cfg.allowed_authors:
            print(f"Merge queue entry for PR #{number} by {author}; not a dependency PR, skipping.")
            set_output("skipped", "true")
            return 0
    set_output("skipped", "false")
    if not cfg.test_command:
        print("::warning::Merge queue run with no test-command: nothing to verify.")
        set_output("verdict", "needs-human")
        return 1 if cfg.fail_on == "needs-human" else 0
    test = run_checks(cfg, cfg.runner_temp / "dep-review-merge-queue.log")
    verdict = PASS if test.passed else "fail"
    set_output("verdict", verdict)
    append_summary(f"Merge queue check: `{test.command}` {test.describe()} on {ref or 'the queued commit'}.")
    return 1 if should_fail(verdict, cfg.fail_on) else 0


def run_test_job(cfg: Config, event: dict) -> int:
    """mode: test. Build and test the PR, and leave the result for the mode: review job.

    Holds no credentials: the PR's code runs here. Everything comes from the event."""
    pr = event.get("pull_request") or {}
    number = pr.get("number")
    if not number:
        raise ConfigError("mode: test runs on the pull_request event (workflow_dispatch isn't supported)")
    author = ((pr.get("user") or {}).get("login") or "").lower()
    if author not in cfg.allowed_authors:
        print(f"PR #{number} is by {author}, not one of {', '.join(cfg.allowed_authors)}; skipping.")
        set_output("skipped", "true")
        return 0
    set_output("skipped", "false")

    head_sha, base_ref = pr["head"]["sha"], pr["base"]["ref"]
    checkout = describe_checkout(cfg.workspace, head_sha, base_ref)
    print(f"Workspace: {checkout.description}")
    cfg.test_result_dir.mkdir(parents=True, exist_ok=True)
    test = run_checks(cfg, cfg.test_result_dir / LOG_FILE)
    write_test_result(cfg.test_result_dir, pr_number=int(number), head_sha=head_sha, checkout=checkout, test=test)
    append_summary(f"Test job: `{test.command}` {test.describe()} on {checkout.description}. The review job gives the verdict.")
    # The result is data for the review job, which gives the verdict and fails the check;
    # a failing exit here would only skip that job.
    return 0


def review_pr(cfg: Config, gh: GitHub, event: dict) -> int:
    number = cfg.pr_number or (event.get("pull_request") or {}).get("number")
    if not number:
        raise ConfigError("no pull request in the triggering event; set the pr-number input")
    pr = gh.get_pr(int(number))
    author = pr["user"]["login"].lower()
    if author not in cfg.allowed_authors:
        print(f"PR #{number} is by {author}, not one of {', '.join(cfg.allowed_authors)}; skipping.")
        set_output("skipped", "true")
        return 0
    set_output("skipped", "false")

    head_sha, base_sha, base_ref = pr["head"]["sha"], pr["base"]["sha"], pr["base"]["ref"]
    files = gh.list_pr_files(number)
    print(f"Reviewing PR #{number}: {pr['title']} ({len(files)} files changed)")

    test_log = cfg.runner_temp / f"dep-review-test-{number}.log"
    tools = RepoTools(cfg.workspace, base_sha, head_sha)
    tools.ensure_commits()
    checkout = describe_checkout(cfg.workspace, head_sha, base_ref)
    print(f"Workspace: {checkout.description}")
    if not checkout.is_merge_result:
        print(
            "::warning::The workspace is not the PR's merge result, so this can't verify what will land. "
            "Use actions/checkout without `ref:` on the pull_request event."
        )

    if cfg.mode == "review":
        # The tests ran in the mode: test job; this job never runs the PR's code.
        try:
            result = load_test_result(cfg.test_result_dir, pr_number=int(number), head_sha=head_sha,
                                      log_tail_lines=LOG_TAIL_LINES)
        except TestResultError as e:
            print(f"::warning::No usable test result from the test job: {e}")
            test, tools.test_log = None, None
        else:
            test, tools.test_log = result.check, result.log_path
            checkout = checkout_for_review(checkout, result.checkout_sha)
            print(f"Test job: `{test.command}` {test.describe()} on {result.checkout_sha[:7]}")
    else:
        if cfg.anthropic_api_key and cfg.test_command:
            print(
                "::warning::test-command runs in the same job as the API key, so the PR's code could read "
                "the key. Prefer two jobs, mode: test and mode: review (see README)."
            )
        test = run_checks(cfg, test_log) if cfg.test_command else None
        if test is None:
            print("::warning::No test-command set: the verdict can't be a pass without a build and tests.")
        tools.test_log = test_log if test else None

    user_message = build_user_message(pr, files, test, checkout.description)
    try:
        if cfg.uses_claude_code:
            outcome = run_review_cli(
                oauth_token=cfg.claude_code_oauth_token,
                model=cfg.model,
                effort=cfg.effort,
                max_turns=cfg.max_turns,
                system=SYSTEM_PROMPT,
                user_message=user_message,
                tools=tools,
                work_dir=cfg.runner_temp / f"dep-review-claude-code-{number}",
                web_fetch_domains=cfg.web_fetch_domains,
                claude_bin=cfg.claude_bin,
            )
        else:
            outcome = run_review(
                anthropic.Anthropic(api_key=cfg.anthropic_api_key),
                model=cfg.model,
                effort=cfg.effort,
                max_turns=cfg.max_turns,
                system=SYSTEM_PROMPT,
                user_message=user_message,
                tools=tools,
            )
    except ReviewError as e:
        print(f"::warning::Claude review incomplete: {e}")
        outcome = ReviewOutcome(review=None, unavailable_reason=f"Claude's review could not be completed: {e}")
    except anthropic.APIError as e:
        # Report API failures on the PR rather than leaving it silently unreviewed.
        print(f"::warning::Anthropic API error: {e}")
        outcome = ReviewOutcome(review=None, unavailable_reason=f"The Claude API returned an error: {type(e).__name__}.")

    u = outcome.usage
    if u:
        print(
            f"Tokens: input={u.get('input_tokens', 0)} cache_read={u.get('cache_read_input_tokens', 0)} "
            f"cache_write={u.get('cache_creation_input_tokens', 0)} output={u.get('output_tokens', 0)}"
        )

    verdict = decide(outcome, test, checkout)
    print(f"Verdict: {verdict.status}")
    for reason in verdict.reasons:
        print(f"  - {reason}")

    run_url = None
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = f"{cfg.server_url}/{cfg.repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    decision = None
    if cfg.auto_merge:
        try:
            update = parse_dependabot_metadata(gh.get_commit_message(head_sha))
        except GitHubError as e:
            print(f"::warning::Could not read Dependabot's commit metadata: {e}")
            update = parse_dependabot_metadata("")  # unknown update type, so no auto-merge
        decision = auto_merge_decision(verdict, outcome, update, cfg.auto_merge_update_types)
        print(f"Auto-merge: {'yes' if decision.merge else 'no'}, {decision.reason}")
    body = render_comment(outcome, verdict, RenderContext(cfg.server_url, cfg.repository, cfg.model, checkout, test, run_url))
    if decision is not None:
        body += f"\n**Auto-merge:** {'✅ yes' if decision.merge else '✋ no'}, {decision.reason}.\n"
    body += ticket_text(cfg, pr, verdict, outcome, run_url)
    append_summary(body)

    try:
        if cfg.post_comment:
            print(f"Comment: {gh.upsert_comment(number, body, COMMENT_MARKER)}")
        if cfg.add_labels:
            label = f"{cfg.label_prefix}{verdict.status}"
            gh.ensure_label(label, LABEL_COLORS[verdict.status], LABEL_DESCRIPTIONS[verdict.status])
            gh.set_exclusive_label(number, [l["name"] for l in pr.get("labels", [])], cfg.label_prefix, label)
    except GitHubError as e:
        if e.status == 403:
            print(
                "::error::GitHub refused to post the comment or label (403). Give the workflow "
                "`pull-requests: write` and `issues: write` permissions."
            )
        raise

    merged = False
    if decision is not None:
        merged = update_auto_merge(
            gh, pr, decision.merge, cfg.auto_merge_method, checkout,
            # mode: review never runs the PR's code, so a moved base isn't re-tested here;
            # merge_directly then leaves the PR for the next run.
            retest=None if cfg.mode == "review" else (lambda base: retest_on_base(cfg, number, head_sha, base)),
        )

    set_output("verdict", verdict.status)
    set_output("auto-merge", "true" if decision and decision.merge else "false")
    set_output("merged", "true" if merged else "false")
    set_output("risk", outcome.risk)
    set_output("confidence", outcome.confidence)

    if should_fail(verdict.status, cfg.fail_on):
        print(f"::error::Verdict is {verdict.status} (fail-on: {cfg.fail_on})")
        return 1
    return 0


def ticket_text(cfg: Config, pr: dict, verdict: Verdict, outcome: ReviewOutcome, run_url: str | None) -> str:
    """Ticket title and description to paste into Linear or Jira, when the verdict calls for one."""
    if not should_fail(verdict.status, cfg.ticket_on):
        return ""
    review = outcome.review or {}
    untested = [
        f"{i['file']}:{i['line']}: {i['description']}"
        for i in review.get("impacted_code", [])
        if i.get("covered_by_tests") != "yes"
    ]
    content = build_content(
        repository=cfg.repository, pr=pr, verdict=verdict.status, reasons=verdict.reasons,
        summary=review.get("summary"), untested=untested, pr_url=pr["html_url"], run_url=run_url,
    )
    return render_ticket_section(content)


CHECK_WAIT_SECONDS = 20 * 60
CHECK_POLL_SECONDS = 20
# How many times a run re-tests on a moved base branch before leaving the PR for a human.
MAX_RETESTS = 3
RETEST_MARKER = "<!-- claude-dependabot-reviewer:retest -->"

# Re-runs test-command on the PR merged into a newer base commit; True if it passed.
Retest = Callable[[str], bool]


def update_auto_merge(gh: GitHub, pr: dict, merge: bool, method: str, checkout: Checkout,
                      retest: Retest | None = None, sleep=time.sleep, clock=time.monotonic) -> bool:
    """Hand the PR to GitHub auto-merge, or merge it directly where that isn't possible.

    Returns True if this run merged the PR itself.
    """
    if not merge:
        if pr.get("auto_merge"):
            try:
                gh.disable_auto_merge(pr["node_id"])
                print("Auto-merge disabled: this push no longer qualifies.")
            except GitHubError as e:
                print(f"::warning::Could not disable auto-merge: {e}")
        return False
    try:
        gh.enable_auto_merge(pr["node_id"], method)
        print(f"Auto-merge enabled ({method}); GitHub merges once all required checks pass.")
        return False
    except GitHubError as e:
        # Typically "clean status": no required checks to wait for (no branch protection,
        # e.g. a private repo on GitHub Free), or auto-merge is off in the repo settings.
        print(f"GitHub auto-merge unavailable ({e}); merging directly once the other checks pass.")
    return merge_directly(gh, pr, method, checkout, retest, sleep, clock)


def merge_directly(gh: GitHub, pr: dict, method: str, checkout: Checkout, retest: Retest | None,
                   sleep, clock) -> bool:
    number, head_sha = pr["number"], pr["head"]["sha"]
    tested_base = checkout.base_sha
    if not tested_base:
        print("Not merging: the tests didn't run on the PR's merge result.")
        return False
    own_run = os.environ.get("GITHUB_RUN_ID")
    deadline = clock() + CHECK_WAIT_SECONDS
    retests = 0
    try:
        while True:
            pending, failed = gh.other_checks(head_sha, own_run)
            if failed:
                print(f"::warning::Not merging: other checks failed: {', '.join(failed)}")
                return False
            if pending:
                if clock() >= deadline:
                    print(f"::warning::Not merging: checks still running after {CHECK_WAIT_SECONDS // 60} min: {', '.join(pending)}")
                    return False
                print(f"Waiting for: {', '.join(pending)}")
                sleep(CHECK_POLL_SECONDS)
                continue

            # Merge only what was tested: the same PR head on the same base commit.
            current = gh.get_pr(number)
            if current["state"] != "open" or current.get("merged"):
                print("PR is no longer open; nothing to merge.")
                return False
            if current["head"]["sha"] != head_sha:
                print("Not merging: the PR has new commits since this review; the next run decides.")
                return False
            new_base = current["base"]["sha"]
            if new_base != tested_base:
                # Usually another dependency PR merged first. The PR's diff (what Claude
                # reviewed) is unchanged, so only the build and tests need re-running.
                if retest is None or retests >= MAX_RETESTS:
                    print("::warning::Not merging: the base branch moved since the tests ran.")
                    return False
                retests += 1
                print(f"Base branch moved to {new_base[:7]}; re-testing the PR merged into it ({retests}/{MAX_RETESTS}).")
                if not retest(new_base):
                    print(f"::warning::Not merging: tests fail with the PR merged into {new_base[:7]}.")
                    gh.upsert_comment(number, (
                        f"{RETEST_MARKER}\n**Not auto-merged:** the review passed, but the tests fail once this PR "
                        f"is merged with the latest base branch ({new_base[:7]}). This usually means it clashes "
                        "with another update that merged first. Check the workflow run for the test output."
                    ), RETEST_MARKER)
                    return False
                tested_base = new_base
                continue

            gh.merge_pr(number, head_sha, method)
            print(f"Merged PR #{number} ({method}).")
            return True
    except GitHubError as e:
        print(f"::warning::Could not merge: {e}")
        return False


def retest_on_base(cfg: Config, number: int, head_sha: str, base_sha: str) -> bool:
    """Check out `base_sha`, merge the PR head into it (as GitHub would), and re-run test-command."""
    host = urllib.parse.urlparse(cfg.server_url).netloc
    basic = base64.b64encode(f"x-access-token:{cfg.github_token}".encode()).decode()
    # The checkout keeps no credentials, so pass the token for this fetch only, through
    # the environment (not argv, not .git/config).
    auth_env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }

    def git(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cfg.workspace, capture_output=True, text=True, env=env)

    fetched = git("fetch", "--no-tags", "--quiet", "origin", base_sha, env=auth_env)
    if fetched.returncode != 0:
        print(f"::warning::Could not fetch {base_sha[:7]}: {fetched.stderr.strip()[:300]}")
        return False
    if git("checkout", "--quiet", "--force", "--detach", base_sha).returncode != 0:
        print(f"::warning::Could not check out {base_sha[:7]}")
        return False
    # A throwaway local merge commit, only for running the tests; it is never pushed, so it
    # is not signed (a runner's signing setup would otherwise make the merge fail).
    merged = git("-c", "user.name=dependabot-gate", "-c", "user.email=dependabot-gate@users.noreply.github.com",
                 "-c", "commit.gpgsign=false", "merge", "--no-ff", "--no-edit", "--quiet", head_sha)
    if merged.returncode != 0:
        git("merge", "--abort")
        print(f"::warning::The PR no longer merges cleanly into {base_sha[:7]}: {(merged.stderr or merged.stdout).strip()[:300]}")
        return False
    if not cfg.test_command:
        return False
    return run_checks(cfg, cfg.runner_temp / f"dep-review-retest-{number}.log").passed


def main() -> int:
    try:
        cfg = Config.from_env()
        gh = GitHub(cfg.github_token, cfg.api_url, cfg.repository, cfg.graphql_url)
        event = load_event(cfg)
        if cfg.event_name == "merge_group":
            if cfg.mode == "review":
                print("Merge queue: the mode: test job re-runs the tests; nothing to review.")
                return 0
            return run_merge_queue(cfg, gh, event)
        if cfg.mode == "test":
            return run_test_job(cfg, event)
        return review_pr(cfg, gh, event)
    except ConfigError as e:
        print(f"::error::{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
