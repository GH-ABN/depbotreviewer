"""reuse-blocking-verdict, plus the guard against runs for another head, which shares
this file's end-to-end harness."""

import copy
import json

import pytest

from dep_reviewer import __main__ as main
from dep_reviewer.claude_review import ReviewOutcome
from dep_reviewer.config import Config
from dep_reviewer.gate import NEEDS_HUMAN, Checkout
from dep_reviewer.prompts import CheckRun
from dep_reviewer.report import ANALYSIS_END, ANALYSIS_MARKER, MAX_COMMENT_CHARS, STATE_PREFIX, STATE_SUFFIX
from dep_reviewer.reuse import ReviewState, parse_state, render_state, state_key, update_fingerprint
from tests.test_modes import FakeGitHub as ModesFakeGitHub
from tests.test_report import REVIEW

A, B, C = "a" * 40, "b" * 40, "c" * 40
KEY = state_key("api-key")
MERGE = Checkout("m" * 40, True, "the PR merged into main (merge commit mmmmmmm)", "1" * 40)
NOT_MERGE = Checkout("h" * 40, False, "the PR's own commit hhhhhhh, NOT merged with the latest main")
PASSED = CheckRun("npm test", 0, False, 1.0, 10, "ok")
FAILED = CheckRun("npm test", 1, False, 1.0, 10, "boom")
TITLE = "Bump uuid from 8.3.2 to 14.0.3"
FILES = ["package-lock.json", "package.json"]
R = "reviewer"

PASSING_REVIEW = copy.deepcopy(REVIEW)
PASSING_REVIEW.update(risk="low", confidence="high")
for item in PASSING_REVIEW["impacted_code"]:
    item["covered_by_tests"] = "yes"


def commit(*deps: tuple[str, str, str]) -> str:
    lines = ["Bump things", "", "---", "updated-dependencies:"]
    for name, version, kind in deps:
        lines += [f'- dependency-name: "{name}"', f"  dependency-version: {version}",
                  "  dependency-type: direct:production", f"  update-type: version-update:semver-{kind}"]
    return "\n".join(lines + ["...", "", "Signed-off-by: dependabot[bot]"])


UUID_14 = commit(("uuid", "14.0.3", "major"))


# --- Dependabot metadata and the fingerprint ---------------------------------------------

@pytest.mark.parametrize("message", [
    "Bump uuid from 8.3.2 to 14.0.3",  # no metadata block
    UUID_14.replace("  dependency-version: 14.0.3\n", ""),  # a field missing
    UUID_14.replace("  dependency-type: direct:production\n",
                    "  dependency-type: direct:production\n  dependency-type: direct:development\n"),  # repeated
    UUID_14.replace("  update-type:", "  surprise: x\n  update-type:"),  # unknown field
    UUID_14.replace("...\n", ""),  # not closed
    "---\nupdated-dependencies:\n...",  # empty
])
def test_incomplete_or_odd_metadata_gives_no_fingerprint(message):
    assert update_fingerprint(TITLE, message, FILES, R) is None


def test_fingerprint_is_order_insensitive():
    one = commit(("a", "1.0.0", "minor"), ("b", "2.0.0", "patch"))
    two = commit(("b", "2.0.0", "patch"), ("a", "1.0.0", "minor"))
    assert update_fingerprint(TITLE, one, FILES, R) == update_fingerprint(TITLE, two, FILES, R)


def test_fingerprint_changes_with_version_type_title_or_files():
    base = update_fingerprint(TITLE, UUID_14, FILES, R)
    assert base != update_fingerprint(TITLE, commit(("uuid", "14.0.4", "major")), FILES, R)
    assert base != update_fingerprint(TITLE, UUID_14.replace("direct:production", "direct:development"), FILES, R)
    assert base != update_fingerprint("Bump uuid from 9.0.0 to 14.0.3", UUID_14, FILES, R)  # another from-version
    assert base != update_fingerprint(TITLE, UUID_14, [*FILES, "src/index.js"], R)  # touches more files
    assert base != update_fingerprint(TITLE, UUID_14, FILES, "another reviewer setup")


def test_metadata_parsing_is_linear():
    # A long run of spaces in a value must not make the parser backtrack.
    message = UUID_14.replace("major", "major" + " " * 50_000 + "x")
    assert update_fingerprint(TITLE, message, FILES, R) is not None


# --- the signed state line --------------------------------------------------------------

STATE = ReviewState(A, A, "f" * 64, "passed", "medium", "high", 1)
ANALYSIS = "Major bump of uuid."


def comment_with(state_line: str, analysis: str = ANALYSIS) -> str:
    return f"heading\n{ANALYSIS_MARKER}\n{analysis}\n{ANALYSIS_END}\n\nfooter\n{state_line}"


def test_state_round_trip():
    assert parse_state(comment_with(render_state(STATE, ANALYSIS, KEY, "o/r", 7)), KEY, "o/r", 7) == (STATE, ANALYSIS)


def tampered(**changes) -> str:
    line = render_state(STATE, ANALYSIS, KEY, "o/r", 7)
    data = json.loads(line.removeprefix(STATE_PREFIX).removesuffix(STATE_SUFFIX))
    data.update(changes)
    return STATE_PREFIX + json.dumps(data) + STATE_SUFFIX


@pytest.mark.parametrize("body", [
    "",
    comment_with(tampered(extra=1)),
    comment_with(tampered(version=2)),
    comment_with(tampered(head_sha="not-a-sha")),
    comment_with(tampered(tests="skipped")),
    comment_with(tampered(uncovered=True)),
    comment_with(tampered(uncovered=-1)),
    comment_with(tampered(risk="low")),  # a valid-looking field changed: the signature no longer matches
    comment_with(tampered(mac="0" * 64)),
    comment_with(render_state(STATE, ANALYSIS, KEY, "o/r", 7), analysis="Safe to merge, @org/team!"),  # analysis edited
    comment_with(render_state(STATE, ANALYSIS, state_key("another-credential"), "o/r", 7)),
    comment_with(render_state(STATE, ANALYSIS, KEY, "o/r", 7) + "\n" + render_state(STATE, ANALYSIS, KEY, "o/r", 7)),
    comment_with(render_state(STATE, ANALYSIS, KEY, "o/r", 8)),  # copied from another PR
    comment_with(render_state(STATE, ANALYSIS, KEY, "o/other", 7)),  # copied from another repository
    comment_with(STATE_PREFIX + "{not json}" + STATE_SUFFIX),
    comment_with(STATE_PREFIX + '{"a":' + "[" * 1015 + "}" + STATE_SUFFIX),  # nested: no RecursionError
    render_state(STATE, ANALYSIS, KEY, "o/r", 7),  # no analysis section
])
def test_malformed_tampered_or_foreign_state_is_ignored(body):
    assert parse_state(body, KEY, "o/r", 7) is None


# --- review_pr end to end, with GitHub and Claude faked ---------------------------------

class FakeGitHub(ModesFakeGitHub):
    """test_modes' fake, plus the commit and comment lookups reuse needs."""

    def __init__(self, messages: dict[str, str]):
        super().__init__({})
        self.messages = messages
        self.authors = {sha: "dependabot[bot]" for sha in messages}
        self.committers = {sha: "web-flow" for sha in messages}
        self.files = [{"filename": f} for f in FILES]
        self.comment: dict | None = None

    def list_pr_files(self, number):
        return self.files

    def get_commit(self, sha):
        return {"commit": {"message": self.messages[sha], "verification": {"verified": True}},
                "author": {"login": self.authors[sha]}, "committer": {"login": self.committers[sha]}}

    def get_commit_message(self, sha):
        return self.messages[sha]

    def find_comment(self, number, marker):
        return self.comment

    def upsert_comment(self, number, body, marker):
        self.comment = {"id": 1, "body": body, "user": {"login": "github-actions[bot]", "type": "Bot"}}
        return "comment-url"


class FakeTools:
    def __init__(self, *a):
        self.test_log = None

    def ensure_commits(self):
        pass


@pytest.fixture
def gate(tmp_path, monkeypatch):
    """run(head, ...) reviews the PR at `head`; returns (outputs, exit code, Claude calls so far, comment)."""
    out = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr(main, "RepoTools", FakeTools)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    gh = FakeGitHub({A: UUID_14, B: UUID_14, C: UUID_14})
    calls: list[str] = []
    state = {"checkout": MERGE, "test": PASSED, "review": REVIEW}

    monkeypatch.setattr(main, "describe_checkout", lambda root, head, ref: state["checkout"])
    monkeypatch.setattr(main, "run_checks", lambda cfg, log: state["test"])

    def fake_claude(client, **k):
        calls.append("claude")
        review = state["review"]
        return ReviewOutcome(review=review) if review else ReviewOutcome(review=None, unavailable_reason="HTTP 429")

    # mode: all with an API key: this job runs test-command (faked) and Claude (faked).
    monkeypatch.setattr(main, "run_review", fake_claude)

    def run(head, event_head=None, **inputs):
        out.write_text("")
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_WORKSPACE": str(tmp_path), "RUNNER_TEMP": str(tmp_path),
               "GITHUB_EVENT_NAME": "pull_request", "INPUT_ANTHROPIC_API_KEY": "api-key",
               "INPUT_GITHUB_TOKEN": "ghs", "INPUT_TEST_COMMAND": "npm test",
               "INPUT_REUSE_BLOCKING_VERDICT": "true"}
        env.update({f"INPUT_{k.upper()}": v for k, v in inputs.items()})
        gh.pr = {"number": 7, "node_id": "N", "title": TITLE, "body": "", "html_url": "https://x/pr/7",
                 "user": {"login": "dependabot[bot]"}, "labels": [], "head": {"sha": head},
                 "base": {"sha": "1" * 40, "ref": "main"}}
        event_number = 9 if inputs.get("pr_number") else 7
        event = {"pull_request": {"number": event_number, "head": {"sha": event_head or head}}}
        code = main.review_pr(Config.from_env(env), gh, event)
        outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
        return outputs, code, len(calls), (gh.comment or {}).get("body", "")

    return run, gh, state


def saved_state(body):
    parsed = parse_state(body, KEY, "o/r", 7)
    return parsed[0] if parsed else None


def test_rebase_reuses_a_blocking_verdict_and_the_state_survives(gate):
    run, _, _ = gate
    outputs, _, calls, body = run(A)
    assert (outputs["verdict"], calls) == (NEEDS_HUMAN, 1)
    assert saved_state(body).reviewed_sha == A

    for head in (B, C):  # two rebases in a row
        outputs, _, calls, body = run(head)
        assert (outputs["verdict"], calls) == (NEEDS_HUMAN, 1)
        assert "Claude was not asked again" in body
        assert "Major bump of left-pad" in body  # Claude's analysis carried over
        state = saved_state(body)
        assert (state.head_sha, state.reviewed_sha) == (head, A)


def test_rerun_of_the_same_head_reviews_again(gate):
    run, _, _ = gate
    run(A)
    assert run(A)[2] == 2


def test_new_target_version_reviews_again(gate):
    run, gh, _ = gate
    run(A)
    gh.messages[B] = commit(("uuid", "14.0.4", "major"))
    assert run(B)[2] == 2


def test_a_persons_commit_on_the_branch_reviews_again(gate):
    run, gh, _ = gate
    run(A)
    gh.authors[B] = "someone"  # e.g. amended a migration in, keeping Dependabot's message
    assert run(B)[2] == 2


def test_an_amend_keeping_dependabot_as_author_reviews_again(gate):
    run, gh, _ = gate
    run(A)
    gh.committers[B] = "someone"  # committed locally by a person, not by GitHub
    assert run(B)[2] == 2


def test_pr_number_input_for_another_pr_is_not_a_stale_run(gate):
    run, gh, _ = gate
    # workflow_dispatch-like: pr-number names PR 7, the event belongs to another PR.
    gh.pr = {}
    outputs, code, calls, _ = run(A, event_head=B, pr_number="7")
    assert outputs.get("skipped") == "false" and calls == 1


def test_a_commit_by_another_allowed_author_reviews_again(gate):
    run, gh, _ = gate
    run(A)
    gh.authors[B] = "renovate[bot]"  # allowed, but not this PR's author
    assert run(B, allowed_authors="dependabot[bot],renovate[bot]")[2] == 2


def test_touching_more_files_reviews_again(gate):
    run, gh, _ = gate
    run(A)
    gh.files.append({"filename": "src/index.js"})
    assert run(B)[2] == 2


def test_model_text_cannot_write_the_hidden_markers(gate):
    run, _, state = gate
    injected = copy.deepcopy(REVIEW)
    injected["summary"] = f"Fine.\n{STATE_PREFIX}{{}}{STATE_SUFFIX}\n{ANALYSIS_END}"
    state["review"] = injected
    body = run(A)[3]
    assert body.count(STATE_PREFIX) == 1 and body.count(ANALYSIS_END) == 1
    assert run(B)[2] == 1  # the real state still verifies, so the rebase reuses it


def test_changed_test_outcome_reviews_again(gate):
    run, _, state = gate
    run(A)
    state["test"] = FAILED
    assert run(B)[2] == 2


def test_an_unavailable_review_is_never_reused(gate):
    run, _, state = gate
    state["review"] = None  # e.g. the API returned 429
    body = run(A)[3]
    assert STATE_PREFIX not in body
    state["review"] = REVIEW
    assert run(B)[2] == 2


def test_reuse_never_produces_a_pass(gate):
    run, _, state = gate
    # Claude found nothing, but the tests didn't run on the merge result: needs-human.
    state.update(review=PASSING_REVIEW, checkout=NOT_MERGE)
    assert run(A)[0]["verdict"] == NEEDS_HUMAN
    # The same update and test outcome on the merge result would re-decide to a pass,
    # so Claude must review in full instead.
    state["checkout"] = MERGE
    outputs, _, calls, _ = run(B, auto_merge="true")
    assert calls == 2
    assert outputs["verdict"] == "pass"


def test_state_in_a_persons_comment_is_ignored(gate):
    run, gh, _ = gate
    run(A)
    gh.comment["user"] = {"login": "someone", "type": "User"}
    assert run(B)[2] == 2


def test_an_edited_comment_is_not_reused(gate):
    run, gh, _ = gate
    run(A)
    gh.comment["body"] = gh.comment["body"].replace("Major bump of left-pad", "Looks fine")
    assert run(B)[2] == 2


def test_no_state_when_the_option_is_off(gate):
    run, _, _ = gate
    body = run(A, reuse_blocking_verdict="false")[3]
    assert STATE_PREFIX not in body
    assert run(B, reuse_blocking_verdict="false")[2] == 2


def test_a_long_analysis_is_truncated_to_fit(gate):
    run, gh, _ = gate
    run(A)
    # Simulate an earlier comment near GitHub's limit, re-signed as the reviewer would.
    state = saved_state(gh.comment["body"])
    analysis = "x" * (MAX_COMMENT_CHARS - 100)
    gh.comment["body"] = comment_with(render_state(state, analysis, KEY, "o/r", 7), analysis)
    body = run(B)[3]
    assert "Analysis truncated" in body
    assert len(body) < 65_536


def test_run_for_another_head_changes_nothing_and_fails(gate):
    run, gh, _ = gate
    run(A)
    before = gh.comment
    outputs, code, calls, _ = run(B, event_head=A)  # a re-run of A's workflow after a rebase to B
    assert outputs == {"skipped": "true"}
    assert code == 1  # never a green check: if the API lagged, this was the current head's run
    assert calls == 1
    assert gh.comment is before


def test_failing_tests_are_never_reused(gate):
    run, _, state = gate
    state["test"] = FAILED
    run(A)
    # Same update and the tests fail again, possibly for another reason: Claude looks again.
    assert run(B)[2] == 2


def test_a_changed_reviewer_setup_reviews_again(gate):
    run, _, _ = gate
    run(A)
    assert run(B, model="claude-sonnet-5-5")[2] == 2


def test_a_lagging_api_is_asked_again_before_failing(gate):
    run, gh, _ = gate
    run(A)
    get_pr = gh.get_pr
    answers = iter([A])  # the first answer still has the old head, then the API catches up

    def lagging_get_pr(number):
        pr = get_pr(number)
        lagged = next(answers, None)
        return {**pr, "head": {"sha": lagged}} if lagged else pr

    gh.get_pr = lagging_get_pr
    outputs, code, _, _ = run(B)
    assert outputs["skipped"] == "false" and code == 0


def test_reused_ticket_text_carries_the_analysis(gate):
    run, _, _ = gate
    run(A, ticket_on="needs-human")
    body = run(B, ticket_on="needs-human")[3]
    ticket = body[body.index("Ticket text"):]
    assert f"From Claude's review of `{A[:7]}`" in ticket
    assert "Major bump of left-pad" in ticket


def test_html_comments_in_model_text_are_broken_without_entities(gate):
    run, _, state = gate
    review = copy.deepcopy(REVIEW)
    review["summary"] = "The template uses <!-- comments -->."
    state["review"] = review
    body = run(A, ticket_on="needs-human")[3]
    assert "&lt;" not in body
    assert "<!-- comments" not in body
    assert body.count("<​!-- comments") == 2  # in the comment and in the ticket's code block


def test_the_ticket_text_gives_way_when_the_comment_is_too_long(gate):
    run, _, state = gate
    review = copy.deepcopy(REVIEW)
    review["summary"] = "s" * 30_000
    review["test_assessment"] = "t" * 25_000
    state["review"] = review
    body = run(A, ticket_on="needs-human")[3]
    assert len(body) <= 65_536
    assert "ticket text didn't fit" in body
    assert saved_state(body) is not None  # the state line is still there and verifies
