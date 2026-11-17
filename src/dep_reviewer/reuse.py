"""Reuse a blocking verdict when Dependabot only rebased the PR (opt-in: reuse-blocking-verdict).

Dependabot rebases every open PR whose lockfile conflicts with the base branch, so each
dependency change merged into the base re-runs the whole gate on every open PR. For an
update that needs a migration the verdict doesn't change, and Claude is asked the same
question again.

With the option on, a run skips Claude when Dependabot only rebased a PR the last review
blocked; README.md, "Skipping Claude when Dependabot only rebased", lists the conditions.

A pass is never reused: it can turn on auto-merge, so it always comes from a full review.

The state is the last line of the reviewer's comment. It is read only from a comment
written by a bot account, and only if its HMAC verifies: the key is derived from the
Claude credential, so neither another bot nor a person editing the comment can forge
the state or the analysis it vouches for. Even a forged state could only keep a PR
blocked. Re-running the job for the same head, or deleting the comment, forces a full
review; so does rotating the credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import asdict, dataclass

from .claude_review import ReviewOutcome
from .gate import PASS, Checkout, Verdict, decide, parse_update_entries
from .github_api import GitHub, GitHubError
from .prompts import CheckRun
from .report import ANALYSIS_END, ANALYSIS_MARKER, COMMENT_MARKER, STATE_PREFIX, STATE_SUFFIX
from .test_result import SHA_RE

VERSION = 1
MAX_STATE_CHARS = 1024
# Only the count matters to the gate; capped so the state line stays small.
MAX_UNCOVERED = 1000
TESTS_OUTCOMES = ("passed", "failed", "timed-out", "none")
RISKS = ("low", "medium", "high")
CONFIDENCES = ("low", "medium", "high")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_STATE_LINE = re.compile(re.escape(STATE_PREFIX) + r"(\{[^\n]*\})" + re.escape(STATE_SUFFIX))
# Versioned on its own: changing it invalidates every saved state, independently of VERSION.
_KEY_CONTEXT = b"claude-dependabot-reviewer reuse-blocking-verdict state v1"
# GitHub's own account, the committer of the verified commits Dependabot makes.
GITHUB_COMMITTER = "web-flow"


def state_key(credential: str) -> bytes | None:
    """The HMAC key for the state, derived from the Claude credential; None without one."""
    if not credential:
        return None
    return hmac.new(credential.encode(), _KEY_CONTEXT, hashlib.sha256).digest()


def reviewer_identity(model: str, effort: str, engine: str, system_prompt: str) -> str:
    """What produced a review: a change to any of these (such as a stronger model, or an
    upgrade that changes the prompt) must not reuse a verdict from the old setup."""
    prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()
    return json.dumps([model, effort, engine, prompt_hash])


def update_fingerprint(pr_title: str, commit_message: str, changed_files: list[str],
                       reviewer: str) -> str | None:
    """A hash of what the PR proposes and who reviews it: its title, the files it changes,
    every dependency's name, target version, dependency type and update type from
    Dependabot's commit metadata, and the reviewer setup (see reviewer_identity).

    The title carries the from-version and the directory. File contents aren't hashed:
    a rebase rewrites the lockfile. None when the metadata is missing or incomplete:
    then nothing proves two heads propose the same update.
    """
    entries = parse_update_entries(commit_message)
    if entries is None:
        return None
    canonical = [reviewer, pr_title, sorted(changed_files), sorted(
        (e["dependency-name"], e["dependency-version"], e["dependency-type"], e["update-type"]) for e in entries
    )]
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


def tests_outcome(test: CheckRun | None) -> str:
    """The build and test result in one word, as saved in the state."""
    if test is None:
        return "none"
    if test.timed_out:
        return "timed-out"
    return "passed" if test.passed else "failed"


@dataclass(frozen=True)
class ReviewState:
    head_sha: str  # the PR head the comment describes
    reviewed_sha: str  # the PR head Claude last reviewed in full
    fingerprint: str
    tests: str
    risk: str
    confidence: str
    uncovered: int  # affected call sites Claude did not find covered by tests


def state_from_review(head_sha: str, fingerprint: str | None, test: CheckRun | None,
                      outcome: ReviewOutcome) -> ReviewState | None:
    """The state to save after a full review, or None when it must not be reused."""
    r = outcome.review
    if r is None or fingerprint is None:
        return None
    uncovered = sum(1 for i in r["impacted_code"] if i["covered_by_tests"] != "yes")
    return ReviewState(head_sha, head_sha, fingerprint, tests_outcome(test), r["risk"], r["confidence"],
                       min(uncovered, MAX_UNCOVERED))


def _mac(key: bytes, state: ReviewState, analysis: str, repository: str, number: int) -> str:
    # The repository and PR number are signed too, so a state can't be copied to another PR
    # (or repository sharing the credential).
    payload = json.dumps([repository, number, asdict(state), analysis], sort_keys=True).encode()
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def render_state(state: ReviewState, analysis: str, key: bytes, repository: str, number: int) -> str:
    """The state line, signed together with the analysis section it was saved with."""
    data = {"version": VERSION, **asdict(state), "mac": _mac(key, state, analysis, repository, number)}
    return STATE_PREFIX + json.dumps(data, separators=(",", ":")) + STATE_SUFFIX


def extract_analysis(body: str) -> str | None:
    """The analysis section of a reviewer comment, without the markers."""
    start = body.find(ANALYSIS_MARKER)
    end = body.find(ANALYSIS_END)
    if start < 0 or end < start:
        return None
    return body[start + len(ANALYSIS_MARKER):end].strip("\n")


def parse_state(body: str, key: bytes, repository: str, number: int) -> tuple[ReviewState, str] | None:
    """The state and analysis saved in a comment body, or None unless exactly one
    well-formed state line is present and its signature verifies."""
    matches = _STATE_LINE.findall(body or "")
    analysis = extract_analysis(body or "")
    if len(matches) != 1 or len(matches[0]) > MAX_STATE_CHARS or analysis is None:
        return None
    # The real state is one flat object. Refusing anything nested before parsing also keeps
    # a deeply nested line from exhausting the parser's recursion limit.
    if matches[0].count("{") != 1 or "[" in matches[0]:
        return None
    try:
        data = json.loads(matches[0])
    except (ValueError, RecursionError):
        return None
    fields = set(ReviewState.__dataclass_fields__)
    if not isinstance(data, dict) or set(data) != {"version", "mac", *fields}:
        return None
    # bool is an int subclass: never accept one where a number is expected.
    if isinstance(data["version"], bool) or data["version"] != VERSION:
        return None
    if not all(isinstance(data[k], str) for k in (*fields - {"uncovered"}, "mac")):
        return None
    uncovered = data["uncovered"]
    if isinstance(uncovered, bool) or not isinstance(uncovered, int) or not 0 <= uncovered <= MAX_UNCOVERED:
        return None
    if not (SHA_RE.match(data["head_sha"]) and SHA_RE.match(data["reviewed_sha"])
            and _HEX64.match(data["fingerprint"]) and _HEX64.match(data["mac"])):
        return None
    if data["tests"] not in TESTS_OUTCOMES or data["risk"] not in RISKS or data["confidence"] not in CONFIDENCES:
        return None
    state = ReviewState(**{k: data[k] for k in fields})
    if not hmac.compare_digest(data["mac"], _mac(key, state, analysis, repository, number)):
        return None
    return state, analysis


def reusable_outcome(prior: ReviewState | None, head_sha: str, fingerprint: str | None,
                     test: CheckRun | None) -> ReviewOutcome | None:
    """Claude's earlier assessment, if this run may reuse it instead of asking again.

    The caller still re-decides with it and must do a full review if that gives a pass.
    """
    if prior is None or fingerprint is None:
        return None
    if prior.head_sha == head_sha:
        return None  # a re-run of the same head asks for a fresh review
    if tests_outcome(test) in ("failed", "timed-out"):
        # Claude's analysis explains the failure in that run's log. After a rebase the
        # tests can fail for another reason (such as a clash with the new base), which only
        # a fresh review sees.
        return None
    if prior.fingerprint != fingerprint or prior.tests != tests_outcome(test):
        return None
    # decide() only counts call sites whose covered_by_tests isn't "yes", so the saved
    # count stands in for the call sites themselves.
    impacted = [{"covered_by_tests": "no"}] * prior.uncovered
    return ReviewOutcome(review={"risk": prior.risk, "confidence": prior.confidence, "impacted_code": impacted})


@dataclass(frozen=True)
class PriorReview:
    fingerprint: str | None  # of this run's head; None when nothing may be reused
    state: ReviewState | None  # saved by the run that wrote the reviewer's comment
    analysis: str  # the analysis section that state vouches for


NO_PRIOR = PriorReview(None, None, "")


@dataclass(frozen=True)
class ReusedVerdict:
    outcome: ReviewOutcome  # the earlier review's gate inputs only
    verdict: Verdict  # re-decided with this run's tests and checkout
    state: ReviewState
    analysis: str


def load_prior_review(gh: GitHub, pr: dict, files: list[dict], key: bytes, repository: str,
                      reviewer: str) -> PriorReview:
    """This head's update fingerprint, and the verified state of the reviewer's comment.

    The fingerprint is only taken from a head commit by the PR's own author (Dependabot)
    that GitHub committed and verified, as it does for Dependabot. So a person's commit on
    the branch, even an amend that keeps Dependabot as the author and its message, gets a
    full review. The state is only read from a comment written by a bot account, and
    parse_state also checks its signature.
    """
    try:
        commit = gh.get_commit(pr["head"]["sha"])
        comment = gh.find_comment(pr["number"], COMMENT_MARKER)
    except GitHubError as e:
        print(f"::warning::Could not read the earlier review, so it is not reused: {e}")
        return NO_PRIOR
    author = ((commit.get("author") or {}).get("login") or "").lower()
    committer = (commit.get("committer") or {}).get("login") or ""
    verified = ((commit.get("commit") or {}).get("verification") or {}).get("verified") is True
    fingerprint = None
    if author == pr["user"]["login"].lower() and committer == GITHUB_COMMITTER and verified:
        fingerprint = update_fingerprint(pr.get("title") or "", commit["commit"]["message"],
                                         [f["filename"] for f in files], reviewer)
    if comment is None or (comment.get("user") or {}).get("type") != "Bot":
        return PriorReview(fingerprint, None, "")
    parsed = parse_state(comment.get("body") or "", key, repository, pr["number"])
    if parsed is None:
        return PriorReview(fingerprint, None, "")
    return PriorReview(fingerprint, *parsed)


def reuse_verdict(prior: PriorReview, head_sha: str, test: CheckRun | None, checkout: Checkout,
                  no_test_reason: str | None) -> ReusedVerdict | None:
    """The earlier verdict, re-decided, when this run may skip Claude. None means: review in full."""
    outcome = reusable_outcome(prior.state, head_sha, prior.fingerprint, test)
    if outcome is None:
        return None
    verdict = decide(outcome, test, checkout, no_test_reason)
    if verdict.status == PASS:
        return None  # a pass can turn on auto-merge, so it always comes from a full review
    return ReusedVerdict(outcome, verdict, prior.state, prior.analysis)
