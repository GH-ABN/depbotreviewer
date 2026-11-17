"""Render the review as a PR comment."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

from .claude_review import ReviewOutcome
from .gate import FAIL, NEEDS_HUMAN, NO_TESTS, PASS, Checkout, Verdict
from .prompts import CheckRun

MARKER_PREFIX = "<!-- claude-dependabot-reviewer"
COMMENT_MARKER = f"{MARKER_PREFIX} -->"
# Claude's analysis sits between these, so a run that reuses the verdict can carry it over
# (see reuse.py); the state line for that run is the last line of the comment.
ANALYSIS_MARKER = f"{MARKER_PREFIX}:analysis -->"
ANALYSIS_END = f"{MARKER_PREFIX}:analysis-end -->"
STATE_PREFIX = f"{MARKER_PREFIX}:state "
STATE_SUFFIX = " -->"
GITHUB_COMMENT_LIMIT = 65_536  # GitHub rejects longer comments
# The rendered review's budget; the auto-merge line, ticket text and state line come after.
MAX_COMMENT_CHARS = 60_000
MAX_ROWS = 40
# Room kept in a reused comment for the auto-merge line, ticket text and state line.
REUSED_HEADROOM = 10_000

VERDICT_HEADING = {
    PASS: "✅ Verified: safe to merge",
    NEEDS_HUMAN: "👀 Needs human review",
    FAIL: "❌ Do not merge",
}
LABEL_COLORS = {PASS: "0e8a16", NEEDS_HUMAN: "fbca04", FAIL: "d93f0b"}
LABEL_DESCRIPTIONS = {
    PASS: "Dependency update verified by build, tests and Claude review",
    NEEDS_HUMAN: "Dependency update needs a human decision before merging",
    FAIL: "Dependency update breaks the build, tests or code",
}
RISK = {"low": "🟢 low", "medium": "🟡 medium", "high": "🔴 high", "unknown": "⚪ unknown"}
AFFECTS = {"yes": "⚠️ yes", "no": "no", "unclear": "❓ unclear"}
COVERED = {"yes": "✅ tested", "no": "❌ no test", "unknown": "❓ test coverage unknown"}


@dataclass(frozen=True)
class RenderContext:
    server_url: str
    repository: str
    model: str
    checkout: Checkout
    test: CheckRun | None
    run_url: str | None = None
    no_test_reason: str | None = None  # why `test` is None, when tests ran but their result was not usable


def neutralize(text: str) -> str:
    """Stop model-written text from @-mentioning people or teams, or writing an HTML comment
    (such as the reviewer's own hidden markers).

    Both are broken with a zero-width space rather than an HTML entity: the text also goes
    into code blocks (the ticket text), where GitHub shows and copies entities literally.
    """
    return re.sub(r"@(?=[A-Za-z0-9_-])", "@​", text).replace("<!--", "<​!--")


def cell(text: str) -> str:
    return neutralize(text).replace("\r", " ").replace("\n", " ").replace("|", "\\|").strip()


def file_link(ctx: RenderContext, path: str, line: int) -> str:
    path = path.removeprefix("./").lstrip("/")
    url = f"{ctx.server_url}/{ctx.repository}/blob/{ctx.checkout.sha}/{urllib.parse.quote(path)}"
    label = path
    if line > 0:
        url += f"#L{line}"
        label += f":{line}"
    return f"[`{cell(label)}`]({url})"


def _safe_url(url: str) -> str:
    return url if re.match(r"^https?://[^\s)]+$", url) else ""


def _tested(ctx: RenderContext) -> str:
    if ctx.test is not None:
        return f"`{ctx.test.command}` {ctx.test.describe()}"
    return ctx.no_test_reason or NO_TESTS


def _header(verdict: Verdict, ctx: RenderContext) -> list[str]:
    """The verdict heading, its reasons and the Tested line; the risk line follows."""
    out = [COMMENT_MARKER, f"## {VERDICT_HEADING[verdict.status]}", ""]
    out += [f"- {neutralize(r)}" for r in verdict.reasons]
    out += ["", f"**Tested:** {_tested(ctx)}, on {ctx.checkout.description}.  "]
    return out


def render_comment(outcome: ReviewOutcome, verdict: Verdict, ctx: RenderContext, max_rows: int = MAX_ROWS) -> str:
    out = _header(verdict, ctx) + [f"**Claude risk rating:** {RISK[outcome.risk]}"]
    r = outcome.review
    if r is not None:
        out[-1] += f" (confidence: {r['confidence']})"
    out.append("")

    if r is not None:
        out += [ANALYSIS_MARKER, neutralize(r["summary"]).strip(), ""]

        if r["dependencies"]:
            out += ["### Updates", "", "| Package | Ecosystem | From | To | Type | Scope |", "|---|---|---|---|---|---|"]
            for d in r["dependencies"]:
                name = cell(d["name"]) + ("" if d["direct"] else " (transitive)")
                out.append(
                    f"| {name} | {cell(d['ecosystem'])} | {cell(d['from_version'])} | {cell(d['to_version'])} "
                    f"| {d['update_type']} | {d['scope']} |"
                )
            out.append("")

        relevant = [c for c in r["upstream_changes"] if c["kind"] not in ("feature", "other") or c["affects_repo"] != "no"]
        if relevant:
            out += ["### Upstream changes that matter", "", "| Package | Kind | Change | Affects this repo |", "|---|---|---|---|"]
            for c in relevant[:max_rows]:
                change = cell(c["description"])
                url = _safe_url(c["source_url"])
                if url:
                    change += f" ([source]({url}))"
                out.append(f"| {cell(c['dependency'])} | {c['kind']} | {change} | {AFFECTS[c['affects_repo']]} |")
            if len(relevant) > max_rows:
                out.append(f"\n_{len(relevant) - max_rows} more not shown._")
            out.append("")

        if r["impacted_code"]:
            # Untested call sites first: they are what stands between this PR and a pass.
            sites = sorted(r["impacted_code"], key=lambda i: i["covered_by_tests"] == "yes")
            out += ["### Affected call sites", ""]
            for i in sites[:max_rows]:
                out.append(
                    f"- {COVERED[i['covered_by_tests']]} · {file_link(ctx, i['file'], i['line'])}: "
                    f"{neutralize(i['description']).strip()}"
                )
            if len(sites) > max_rows:
                out.append(f"- _{len(sites) - max_rows} more not shown._")
            out.append("")

        out += ["### Test assessment", "", neutralize(r["test_assessment"]).strip(), ""]

        if r["recommendations"]:
            out += ["### Recommendations", ""]
            out += [f"- {neutralize(x).strip()}" for x in r["recommendations"]]
            out.append("")
        out += [ANALYSIS_END, ""]

    footer = f"<sub>Reviewed by {cell(ctx.model)} in {outcome.turns} turns"
    if ctx.run_url:
        footer += f" · [workflow run]({ctx.run_url})"
    footer += ". Only passing tests on the merge result can produce a pass. Claude's review can only block or escalate.</sub>"
    out.append(footer)

    body = "\n".join(out)
    if len(body) > MAX_COMMENT_CHARS and max_rows > 5:
        return render_comment(outcome, verdict, ctx, max_rows=max_rows // 2)
    return body


def render_reused_comment(verdict: Verdict, ctx: RenderContext, risk: str, confidence: str,
                          reviewed_sha: str, analysis: str) -> str:
    """The comment for a run that reused an earlier blocking verdict (see reuse.py).

    `analysis` is the signed analysis section of the earlier comment, carried over; the
    heading, reasons and test line describe this run.
    """
    out = _header(verdict, ctx) + [
        f"**Claude risk rating:** {RISK[risk]} (confidence: {confidence}), from the review of `{reviewed_sha[:7]}`",
        "",
        "> Dependabot rebased this PR without changing the update, and the tests had the same outcome, "
        f"so Claude was not asked again. The analysis below is from the review of `{reviewed_sha[:7]}`. "
        "To force a full review, re-run the job.",
        "",
    ]
    footer = "<sub>Verdict re-decided with this run's tests"
    if ctx.run_url:
        footer += f" · [workflow run]({ctx.run_url})"
    footer += ". A pass is never reused: it always comes from a full review.</sub>"
    # Leave room for the auto-merge line, ticket text and state line added after this.
    room = MAX_COMMENT_CHARS - len("\n".join(out)) - len(footer) - REUSED_HEADROOM
    carried = neutralize(analysis)
    if len(carried) > room:
        carried = carried[:max(room, 0)].rstrip() + "\n\n_Analysis truncated; see the earlier workflow run._"
    out += [ANALYSIS_MARKER, carried, ANALYSIS_END, "", footer]
    return "\n".join(out)
