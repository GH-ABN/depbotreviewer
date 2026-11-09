from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

EFFORTS = ("low", "medium", "high", "xhigh", "max")
FAIL_ON = ("fail", "needs-human", "never")
MERGE_METHODS = ("squash", "merge", "rebase")
# all: one job tests and reviews. test / review: two jobs, so the PR's code and the
# credentials never share one (see test_result.py).
MODES = ("all", "test", "review")
# Where the Claude Code engine's WebFetch may go: source hosts and package registries,
# where release notes, changelogs and advisories live. Claude Code's WebFetch can fetch
# any URL Claude writes, unlike the API's web_fetch, which only fetches URLs already in
# the conversation; without a list, injected text could have it send code to any host.
DEFAULT_WEB_FETCH_DOMAINS = (
    "github.com", "raw.githubusercontent.com", "gitlab.com", "bitbucket.org",
    "registry.npmjs.org", "www.npmjs.com", "pypi.org", "www.nuget.org", "crates.io", "docs.rs",
    "pkg.go.dev", "rubygems.org", "central.sonatype.com", "packagist.org", "hex.pm", "hub.docker.com",
)
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")


class ConfigError(Exception):
    pass


def _bool(value: str, name: str) -> bool:
    v = value.strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no", ""):
        return False
    raise ConfigError(f"{name} must be true or false, got {value!r}")


def _int(value: str, name: str, minimum: int) -> int:
    try:
        n = int(value.strip())
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from None
    if n < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return n


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    github_token: str
    model: str
    effort: str
    max_turns: int
    test_command: str
    test_timeout_minutes: int
    post_comment: bool
    add_labels: bool
    label_prefix: str
    fail_on: str
    auto_merge: bool
    auto_merge_method: str
    allowed_authors: tuple[str, ...]
    pr_number: int | None
    repository: str
    api_url: str
    server_url: str
    workspace: Path
    event_path: Path | None
    runner_temp: Path
    event_name: str
    graphql_url: str
    ticket_on: str = "fail"
    auto_merge_update_types: tuple[str, ...] = ("patch", "minor")
    # Claude Code engine: set instead of anthropic_api_key (see claude_code_review.py).
    claude_code_oauth_token: str = ""
    claude_bin: str = "claude"
    web_fetch_domains: tuple[str, ...] = DEFAULT_WEB_FETCH_DOMAINS
    mode: str = "all"
    test_result_dir: Path = Path("/tmp/dep-review-test-result")

    @property
    def uses_claude_code(self) -> bool:
        return bool(self.claude_code_oauth_token)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Config:
        def inp(name: str, default: str = "") -> str:
            return env.get(f"INPUT_{name}", default).strip()

        mode = inp("MODE", "all").lower() or "all"
        if mode not in MODES:
            raise ConfigError(f"mode must be one of {', '.join(MODES)}, got {mode!r}")
        test_command = inp("TEST_COMMAND")
        api_key = inp("ANTHROPIC_API_KEY")
        oauth_token = inp("CLAUDE_CODE_OAUTH_TOKEN")
        token = inp("GITHUB_TOKEN")
        if mode == "test":
            # This job runs the PR's code, which can read any credential this process holds.
            if api_key or oauth_token:
                raise ConfigError(
                    "mode: test runs the PR's code, so it must not receive anthropic-api-key or "
                    "claude-code-oauth-token; pass them to the mode: review job only"
                )
            if not test_command:
                raise ConfigError("mode: test needs test-command")
        else:
            if api_key and oauth_token:
                raise ConfigError("set either anthropic-api-key or claude-code-oauth-token, not both")
            if not api_key and not oauth_token:
                raise ConfigError(
                    "anthropic-api-key and claude-code-oauth-token are both empty. Dependabot-triggered "
                    "runs only see Dependabot secrets: add ANTHROPIC_API_KEY (or CLAUDE_CODE_OAUTH_TOKEN) "
                    "under Settings > Secrets and variables > Dependabot."
                )
            if not token:
                raise ConfigError("github-token is empty")
        if mode == "review" and test_command:
            raise ConfigError("mode: review never runs the PR's code; set test-command on the mode: test job")
        if mode == "all" and oauth_token and test_command:
            raise ConfigError(
                "claude-code-oauth-token can't share a job with test-command: the PR's code could read "
                "the token from this process. Use two jobs, mode: test and mode: review (see README)."
            )

        effort = inp("EFFORT", "high").lower()
        if effort not in EFFORTS:
            raise ConfigError(f"effort must be one of {', '.join(EFFORTS)}, got {effort!r}")

        fail_on = inp("FAIL_ON", "fail").lower() or "fail"
        if fail_on not in FAIL_ON:
            raise ConfigError(f"fail-on must be one of {', '.join(FAIL_ON)}, got {fail_on!r}")
        merge_method = inp("AUTO_MERGE_METHOD", "squash").lower() or "squash"
        if merge_method not in MERGE_METHODS:
            raise ConfigError(f"auto-merge-method must be one of {', '.join(MERGE_METHODS)}, got {merge_method!r}")
        api_url = env.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

        pr_raw = inp("PR_NUMBER")
        repository = env.get("GITHUB_REPOSITORY", "")
        if not repository:
            raise ConfigError("GITHUB_REPOSITORY is not set; run this inside GitHub Actions")

        ticket_on = inp("TICKET_ON", "fail").lower() or "fail"
        if ticket_on not in FAIL_ON:
            raise ConfigError(f"ticket-on must be one of {', '.join(FAIL_ON)}, got {ticket_on!r}")
        update_types = tuple(
            t.strip().lower() for t in (inp("AUTO_MERGE_UPDATE_TYPES", "patch,minor") or "patch,minor").split(",") if t.strip()
        )
        bad = [t for t in update_types if t not in ("patch", "minor", "major")]
        if bad or not update_types:
            raise ConfigError(f"auto-merge-update-types must be a comma-separated list of patch, minor, major; got {bad or '(empty)'}")

        raw_domains = inp("WEB_FETCH_DOMAINS")
        domains = (
            tuple(d.strip().lower() for d in raw_domains.replace("\n", ",").split(",") if d.strip())
            if raw_domains else DEFAULT_WEB_FETCH_DOMAINS
        )
        bad_domains = [d for d in domains if not DOMAIN_RE.match(d)]
        if bad_domains:
            raise ConfigError(f"web-fetch-domains must be host names such as github.com; got {', '.join(bad_domains)}")

        event_path = env.get("GITHUB_EVENT_PATH")
        return cls(
            anthropic_api_key=api_key,
            github_token=token,
            model=inp("MODEL", "claude-opus-5-5") or "claude-opus-5-5",
            effort=effort,
            max_turns=_int(inp("MAX_TURNS", "30") or "30", "max-turns", 1),
            test_command=test_command,
            test_timeout_minutes=_int(inp("TEST_TIMEOUT_MINUTES", "20") or "20", "test-timeout-minutes", 1),
            post_comment=_bool(inp("POST_COMMENT", "true"), "post-comment"),
            add_labels=_bool(inp("ADD_LABELS", "true"), "add-labels"),
            label_prefix=inp("LABEL_PREFIX", "dependabot-review:") or "dependabot-review:",
            fail_on=fail_on,
            auto_merge=_bool(inp("AUTO_MERGE", "false"), "auto-merge"),
            auto_merge_method=merge_method,
            allowed_authors=tuple(
                a.strip().lower() for a in inp("ALLOWED_AUTHORS", "dependabot[bot]").split(",") if a.strip()
            ),
            pr_number=_int(pr_raw, "pr-number", 1) if pr_raw else None,
            repository=repository,
            api_url=api_url,
            server_url=env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/"),
            workspace=Path(env.get("GITHUB_WORKSPACE") or os.getcwd()),
            event_path=Path(event_path) if event_path else None,
            runner_temp=Path(env.get("RUNNER_TEMP") or "/tmp"),
            event_name=env.get("GITHUB_EVENT_NAME", ""),
            graphql_url=env.get("GITHUB_GRAPHQL_URL") or f"{api_url}/graphql",
            ticket_on=ticket_on,
            auto_merge_update_types=update_types,
            claude_code_oauth_token=oauth_token,
            claude_bin=env.get("DEP_REVIEWER_CLAUDE_BIN") or "claude",
            web_fetch_domains=domains,
            mode=mode,
            test_result_dir=Path(env.get("RUNNER_TEMP") or "/tmp") / "dep-review-test-result",
        )


def should_fail(verdict: str, fail_on: str) -> bool:
    """fail-on=fail: only a failing verdict fails the check (the default).
    fail-on=needs-human: anything short of a pass fails the check.
    fail-on=never: the check always succeeds; act on the `verdict` output instead."""
    if fail_on == "never":
        return False
    if fail_on == "needs-human":
        return verdict != "pass"
    return verdict == "fail"
