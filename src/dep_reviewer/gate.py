"""The merge gate: turn evidence (tests, checkout state, Claude's review) into a verdict.

Only evidence can produce a pass. Claude's opinion can block a merge or send it to a
human, but it can never pass a PR on its own.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .claude_review import ReviewOutcome
from .prompts import CheckRun

PASS, NEEDS_HUMAN, FAIL = "pass", "needs-human", "fail"
VERDICTS = (PASS, NEEDS_HUMAN, FAIL)


@dataclass(frozen=True)
class Checkout:
    sha: str
    is_merge_result: bool
    description: str
    base_sha: str | None = None  # the base-branch commit the PR was merged into, when is_merge_result


def describe_checkout(root: Path, head_sha: str, base_ref: str) -> Checkout:
    """Work out whether the workspace holds the PR merged into its base branch."""
    out = subprocess.run(
        ["git", "rev-list", "--parents", "-n", "1", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.split()
    sha, parents = out[0], out[1:]
    if len(parents) == 2 and parents[1] == head_sha:
        return Checkout(sha, True, f"the PR merged into {base_ref} at {parents[0][:7]} (merge commit {sha[:7]})", parents[0])
    if sha == head_sha:
        return Checkout(sha, False, f"the PR's own commit {sha[:7]}, NOT merged with the latest {base_ref}")
    return Checkout(sha, False, f"commit {sha[:7]}, which is neither the PR head nor its merge result")


@dataclass(frozen=True)
class Verdict:
    status: str
    reasons: list[str]


NO_TESTS = "no build or tests were run (set `test-command`)"


def decide(outcome: ReviewOutcome, test: CheckRun | None, checkout: Checkout,
           no_test_reason: str | None = None) -> Verdict:
    """Turn the evidence into a verdict; only passing tests on the merge result can give a pass.

    `no_test_reason` says why `test` is None when tests ran but their result was not usable.
    """
    fail: list[str] = []
    human: list[str] = []

    if test is None:
        why = no_test_reason or NO_TESTS
        human.append(f"{why[0].upper()}{why[1:]}, so nothing proves the update works.")
    elif test.timed_out:
        fail.append(f"`{test.command}` timed out.")
    elif not test.passed:
        fail.append(f"`{test.command}` failed (exit code {test.exit_code}).")

    if not checkout.is_merge_result:
        human.append(f"Tests ran on {checkout.description}, so they do not prove the result of merging.")

    r = outcome.review
    if r is None:
        human.append(outcome.unavailable_reason or "Claude's review could not be completed.")
    else:
        if r["risk"] == "high":
            fail.append("Claude rated the update high risk.")
        elif r["risk"] == "medium":
            human.append("Claude rated the update medium risk.")
        if r["confidence"] == "low":
            human.append("Claude has low confidence in its review.")
        uncovered = [i for i in r["impacted_code"] if i["covered_by_tests"] != "yes"]
        if uncovered:
            human.append(
                f"{len(uncovered)} affected call site{'s are' if len(uncovered) != 1 else ' is'} "
                "not proven to be covered by tests."
            )

    if fail:
        return Verdict(FAIL, fail + human)
    if human:
        return Verdict(NEEDS_HUMAN, human)
    return Verdict(
        PASS,
        [
            "The build and tests passed on the merge result.",
            "Every affected call site Claude found is covered by a passing test.",
        ],
    )


_QUEUE_REF = re.compile(r"/pr-(\d+)-[0-9a-f]+$")


def pr_number_from_queue_ref(ref: str) -> int | None:
    """gh-readonly-queue/main/pr-123-<sha> -> 123 (the last PR in the queue group)."""
    m = _QUEUE_REF.search(ref)
    return int(m.group(1)) if m else None


# --- Auto-merge policy -------------------------------------------------------------------

UPDATE_TYPES = ("patch", "minor", "major")
# Dependabot writes this metadata into its commit messages (what dependabot/fetch-metadata reads):
#   updated-dependencies:
#   - dependency-name: ms
#     dependency-type: direct:production
#     update-type: version-update:semver-minor
_UPDATE_TYPE = re.compile(r"^\s*update-type:\s*version-update:semver-(patch|minor|major)\s*$", re.M)
_DEP_NAME = re.compile(r"^\s*-\s*dependency-name:\s*(\S+)", re.M)
_METADATA_FIELDS = ("dependency-name", "dependency-version", "dependency-type", "update-type")


def parse_update_entries(commit_message: str) -> list[dict[str, str]] | None:
    """Each dependency in the commit's `updated-dependencies:` block, with all four fields above.

    Stricter than parse_dependabot_metadata: the block must end with Dependabot's `...`
    line, and an entry with a missing or repeated field makes the whole block None.
    """
    lines = (commit_message or "").splitlines()
    try:
        start = [line.strip() for line in lines].index("updated-dependencies:") + 1
    except ValueError:
        return None
    entries: list[dict[str, str]] = []
    for line in lines[start:]:
        text = line.strip()
        if text == "...":
            if not entries or any(set(e) != set(_METADATA_FIELDS) for e in entries):
                return None
            return entries
        if text.startswith("- "):
            entries.append({})
            text = text[2:].strip()
        key, sep, value = text.partition(":")
        if not sep or not entries:
            return None
        key, value = key.strip(), value.strip().strip("'\"")
        if key == "dependency-group":
            continue
        if key not in _METADATA_FIELDS or key in entries[-1] or not value:
            return None
        entries[-1][key] = value
    return None  # no closing "..."


@dataclass(frozen=True)
class UpdateInfo:
    names: list[str]
    types: list[str]  # one per dependency; unknown when Dependabot gave none

    @property
    def worst(self) -> str:
        if not self.types or len(self.types) < len(self.names) or "unknown" in self.types:
            return "unknown"
        return max(self.types, key=UPDATE_TYPES.index)


def parse_dependabot_metadata(commit_message: str) -> UpdateInfo:
    names = _DEP_NAME.findall(commit_message or "")
    types = _UPDATE_TYPE.findall(commit_message or "")
    return UpdateInfo(names, types)


@dataclass(frozen=True)
class AutoMergeDecision:
    merge: bool
    reason: str


def auto_merge_decision(verdict: Verdict, outcome: ReviewOutcome, update: UpdateInfo,
                        allowed: tuple[str, ...]) -> AutoMergeDecision:
    """Merge without a human only on a pass, with high confidence, for an allowed update type.

    The update type comes from Dependabot's own metadata, not from Claude.
    """
    if verdict.status != PASS:
        return AutoMergeDecision(False, f"the verdict is {verdict.status}")
    if outcome.confidence != "high":
        return AutoMergeDecision(False, f"Claude's confidence is {outcome.confidence}, not high")
    worst = update.worst
    if worst == "unknown":
        return AutoMergeDecision(False, "Dependabot's commit doesn't say the update type")
    if worst not in allowed:
        return AutoMergeDecision(False, f"it's a {worst} update (auto-merge allows {', '.join(allowed)})")
    return AutoMergeDecision(True, f"{worst} update, tests passed on the merge result, high confidence")
