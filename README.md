# Claude Dependabot Gate

A GitHub Action that decides whether a Dependabot pull request is safe to merge, and only passes it on evidence.

Dependabot's compatibility score tells you how the same update went in *other* public repos. This gate checks it against *your* code, on the commit that will actually land.

## What it does

For each Dependabot PR:

1. **Builds and tests the merge result.** It runs your `test-command` on the PR merged into the latest base branch, not on the PR branch alone.
2. **Has Claude investigate the impact.**
   - Claude reads what changed upstream: release notes, changelogs, migration guides and advisories, using web search and fetch.
   - It finds every place your code uses an API that changed, using read-only code search. It includes call sites it thinks are unaffected.
   - For each of those call sites, it checks whether an existing test actually exercises it.
3. **Gives a verdict:**

| Verdict | When |
|---|---|
| ✅ **pass** | Build and tests passed on the merge result, Claude rates the risk low with medium or high confidence, and every affected call site is covered by a test. |
| 👀 **needs-human** | No tests were run, or the workspace wasn't the merge result, or Claude rates the risk medium, or has low confidence, or found an affected call site with no test, or couldn't finish. |
| ❌ **fail** | The build or tests failed or timed out, or Claude found code the update breaks (high risk). |

Claude's opinion can block a PR or send it to a human, but it can never pass one on its own. A pass always needs passing tests on the merge result.

4. **Reports it:** one PR comment that's updated on every push, a `dependabot-review:pass|needs-human|fail` label, a check result and, optionally, auto-merge.

It works with any ecosystem Dependabot supports (npm, yarn, pnpm, NuGet, pip, Poetry, uv, Go, Maven, Gradle, Cargo, Docker, GitHub Actions and more).

## Setup

1. **Add the API key as a Dependabot secret.** Workflows triggered by Dependabot can't read normal Actions secrets. Add `ANTHROPIC_API_KEY` under **Settings > Secrets and variables > Dependabot**, or once at the organization level.
2. **Add the workflow.** Copy [examples/dependabot-review.yml](examples/dependabot-review.yml) to `.github/workflows/`, then set the toolchain step and `test-command`.
3. **Make the check required.** In branch protection or a ruleset for `main`, require the `dependabot-gate` status check.
4. **Make sure what you tested is what lands.** See the next section.
5. **If `GH-ABN/depbotreviewer` is private:** in its **Settings > Actions > General > Access**, allow repositories in the organization to use it.

## Keeping credentials away from the PR's code

`test-command` runs the PR's code, including the updated dependency's install scripts. Anything running as the runner's user can read the environment of the processes around it through `/proc/<pid>/environ`, so in a single job the dependency can read the credential the action holds, even though the action removes it from the test command's own environment.

`mode` splits the work so they never meet:

- **`mode: test`** runs `test-command` with no credentials (the action refuses `anthropic-api-key` and `claude-code-oauth-token` here, and passes no GitHub token except in a merge queue, where it looks up the queued PR and runs only code that is about to land) and uploads the result as an artifact.
- **`mode: review`** runs in a job that `needs` the test job. It gets the credentials, downloads the result and never runs the PR's code. The result was written in a job that ran that code, so it is checked strictly: only the expected fields and types, the same PR head, and the same merge commit. If the base branch moved between the jobs, the two merge commits differ and the verdict is needs-human. A missing or malformed result counts as no tests.

[examples/dependabot-review-two-jobs.yml](examples/dependabot-review-two-jobs.yml) shows the setup. In branch protection, require both jobs' checks. On pull requests the review job gives the verdict, and the test job fails only when it can't run at all. In a merge queue the test job re-runs the tests and the review job does nothing, so requiring only the review check would let a failing queue commit through.

The default, `mode: all`, keeps everything in one job as before; with an API key it logs a warning. With `claude-code-oauth-token` and a `test-command`, two jobs are required. When a `mode: review` run merges directly (no branch protection) and the base branch has moved since the tests, it doesn't re-test, because that would run the PR's code; it leaves the PR for the next run.

## Using a Claude Code OAuth token instead of an API key

Teams on a Claude subscription can run the gate with a Claude Code OAuth token, from `claude setup-token`, instead of an API key. Set `claude-code-oauth-token` and leave `anthropic-api-key` empty; setting both is an error. Store the token as a Dependabot secret, like the key. Run it with two jobs (see "Keeping credentials away from the PR's code"): the action refuses the token in a job that runs `test-command`.

The review then runs through the Claude Code CLI (`claude -p`), which the action installs with `npm ci` from its own lockfile (`claude-code/package-lock.json`), into a new directory outside the checkout and with your npm configuration ignored. The workflow needs Node and npm, for example from `actions/setup-node`. Everything else is the same: the system prompt, the PR context, the verdict schema and rules, the report and auto-merge. Claude gets the same read-only repository tools, served to Claude Code over MCP, plus Claude Code's own WebSearch and WebFetch.

Claude Code's WebFetch, unlike the API's `web_fetch`, can fetch any URL Claude writes, including one that carries code in its query string. So WebFetch is allowed only for the hosts in `web-fetch-domains`: by default GitHub, GitLab, Bitbucket and the package registries (npm, PyPI, NuGet, crates.io and docs.rs, Go, RubyGems, Maven Central, Packagist, Hex, Docker Hub). A fetch to any other host is refused and logged. Add the hosts where your dependencies publish release notes if they live elsewhere.

Claude Code loads `CLAUDE.md`, settings and hooks from its working directory, and a hook runs a shell command. So the action never starts it in the checkout. It runs in a private directory outside the checkout, with `--restricted`, `--setting-sources ""`, `--strict-mcp-config`, a private config directory and no built-in tools except WebSearch and WebFetch. Its environment holds only the token and basic variables like `PATH` and `HOME`, not the GitHub token.

To check the isolation yourself: make a repository with a hook in `.claude/settings.json`, for example `{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "touch /tmp/HOOK_FIRED"}]}]}}`. Run `claude -p hi --setting-sources project` from inside it, with `CLAUDE_CODE_OAUTH_TOKEN` set even to an invalid value: `/tmp/HOOK_FIRED` appears, because hooks load before authentication. Remove it, then run the engine (`run_review_cli`) with that repository as the checkout: the file doesn't appear.

Usage counts against the subscription that owns the token. Prompt caching and the turn limit work as with the API. The API engine's server-side fallback on refusals isn't available, so a refusal gives `needs-human`.

## Making sure what you tested is what lands

The PR's merge result is computed against `main` *as it was when the check ran*. If other PRs merge afterwards, the tested state is out of date. There are two ways to close that gap:

- **Merge queue (recommended).** Turn on the merge queue for `main`. When a PR enters the queue, GitHub builds the exact commit that will land, and the workflow's `merge_group` trigger re-runs your build and tests on it. Claude isn't called again, so this costs no tokens. The merge queue is available for organization repositories: public ones, and private ones on GitHub Enterprise Cloud.
- **Require branches to be up to date.** Turn on **Require branches to be up to date before merging**. Dependabot PRs then have to be updated with `main` before merging. Each update triggers a fresh check, including a new Claude review.

## Auto-merge: hands-off updates (optional)

With `auto-merge: true`, updates the gate is confident about merge with no human involved. Everything else waits for review, with the reason written in the PR comment.

**A PR merges automatically only when all of these are true:**
1. The verdict is **pass**: the build and tests passed on the merge result, Claude rates the risk low, and every affected call site is tested.
2. Claude's confidence is **high**. The verdict alone accepts medium.
3. The update type is in `auto-merge-update-types`, which defaults to `patch,minor`. The type comes from the metadata Dependabot writes into its commit message, not from Claude. A grouped PR counts as its largest update. A PR with no metadata, such as one from another bot, is never merged automatically.

The PR comment says which way it went, for example *"Auto-merge: ✋ no, it's a major update"*.

**How the merge happens:**
- **With branch protection** (the gate is a required check), the gate turns on GitHub auto-merge, and GitHub merges once every required check is green. That includes the merge queue if you use one. If a later push stops qualifying, the gate turns auto-merge off again.
- **Without branch protection** (for example a private repo on GitHub Free, where required checks aren't available), the gate merges the PR itself, with these safeguards:
  - It waits for the PR's other checks, such as CI, for up to 20 minutes. It merges only if they all pass.
  - It merges only the PR head it reviewed. If anything has been pushed since, the next run decides.
  - If `main` has moved since the tests ran (usually because another update merged first), it re-runs `test-command` on the PR merged into the new `main`. Claude isn't called again, because the PR's diff hasn't changed. If the tests now fail, it doesn't merge, and it explains why on the PR.
  - When the gate merges directly, it waits only for checks from other workflows, not for other jobs in its own workflow run. Keep the gate in a workflow of its own.

**What it needs:**
- `contents: write` permission in the workflow.
- With branch protection: **Allow auto-merge** turned on in the repository settings.
- If you deploy on push to `main`: merges made with the default `GITHUB_TOKEN` don't trigger `push` workflows. Pass a GitHub App token or a fine-grained PAT as `github-token`.

### Recommended Dependabot settings

[examples/dependabot.yml](examples/dependabot.yml) is set up to work with auto-merge:
- **Cooldown:** Dependabot waits a few days before proposing a new release. Most malicious or broken releases are caught and pulled within that time, and this matters more once nobody reviews the merge.
- **Grouping:** minor and patch updates arrive in one PR per ecosystem, and majors arrive one at a time. One PR means one review and one merge. Separate PRs would each have to be re-tested after every merge.

## Ticket text for Linear or Jira

When a PR fails the gate, the PR comment ends with a collapsed **📋 Ticket text** section. It holds a title and a Markdown description, each in its own code block, so GitHub's copy button grabs either one in a click. Paste them into Linear, Jira or any other tracker: both Linear and Jira Cloud turn pasted Markdown into formatted text.

The description has the verdict and its reasons, Claude's summary, the affected code with no test coverage, and links to the PR and the workflow run.

`ticket-on` decides which verdicts get ticket text, with the same values as `fail-on`. The default, `fail`, adds it only when the update breaks something. `needs-human` also adds it to updates that couldn't be verified. `never` turns it off.

No tracker credentials are needed, because the action never calls Linear or Jira.

## Inputs

| Input | Default | Description |
|---|---|---|
| `mode` | `all` | `all`: one job tests and reviews. `test` / `review`: two jobs, so the PR's code and the credentials never meet. See "Keeping credentials away from the PR's code". |
| `test-artifact` | `dependabot-gate-test` | Artifact name for the test result passed from the test job to the review job. |
| `anthropic-api-key` | (empty) | Anthropic API key. Set this or `claude-code-oauth-token`. |
| `claude-code-oauth-token` | (empty) | Claude Code OAuth token, used instead of an API key. See "Using a Claude Code OAuth token". |
| `web-fetch-domains` | (built-in list) | Claude Code engine only: hosts WebFetch may read. See "Using a Claude Code OAuth token". |
| `github-token` | `github.token` | Token used to read the PR and write the comment, labels and auto-merge. |
| `test-command` | (empty) | Shell command that builds and tests the PR. **Required for a pass.** |
| `test-timeout-minutes` | `20` | Timeout for `test-command`. A timeout counts as a failure. |
| `fail-on` | `fail` | `fail`: the check fails only on a failing verdict, so people can still merge needs-human PRs after reading the review. `needs-human`: anything short of a pass fails. `never`: the check always succeeds. |
| `auto-merge` | `false` | Merge confident updates without a human. See "Auto-merge". |
| `auto-merge-method` | `squash` | `squash`, `merge` or `rebase`. |
| `auto-merge-update-types` | `patch,minor` | Update types that may merge automatically: any of `patch`, `minor`, `major`. |
| `model` | `claude-opus-5-5` | Claude model. |
| `effort` | `high` | `low`, `medium`, `high`, `xhigh` or `max`. |
| `max-turns` | `30` | Investigation turns before Claude must give its verdict. |
| `post-comment` | `true` | Post or update the PR comment. |
| `add-labels` | `true` | Apply a verdict label. |
| `label-prefix` | `dependabot-review:` | Prefix for verdict labels. |
| `allowed-authors` | `dependabot[bot]` | Comma-separated PR authors to gate. Add `renovate[bot]` for Renovate. |
| `pr-number` | (from the event) | Set this when running from `workflow_dispatch`. |
| `ticket-on` | `fail` | Which verdicts add ticket text for Linear or Jira to the PR comment: `fail`, `needs-human` or `never`. |

**Outputs:** `verdict` (`pass`, `needs-human` or `fail`), `risk` (`low`, `medium`, `high` or `unknown`), `confidence`, `skipped`, `auto-merge` (whether the PR qualified) and `merged` (whether this run merged it).

## Security

- Claude's repository tools are **read-only** and confined to the checkout; they can't read `.git`. Claude has no shell. Your `test-command` is the only thing that runs code. Its own environment has no credentials, but see the next point.
- The PR description, release notes, web pages and repository contents are treated as untrusted data. The prompt tells Claude to ignore instructions inside them and report any such attempt. Text Claude writes into the comment can't @-mention people.
- With `mode: test` and `mode: review`, the PR's code runs in a job that holds no credentials. In a single job (`mode: all`), the dependency being tested can read the credential from the action's process environment.
- The workflow uses `pull_request`, not `pull_request_target`.
- The reviewer runs with Python's `-P` flag, so files in the reviewed repository can't replace the reviewer's own modules.

## Cost

Each review is one Claude conversation of about 5–30 turns. The conversation is prompt-cached, so each turn re-reads earlier context at the cache rate. Token usage is printed in the job log. Merge-queue runs don't call Claude. To spend less, lower `effort` or `max-turns`.

## Requirements

Linux or macOS runners with Python 3.11 or newer. GitHub-hosted `ubuntu-latest` and `macos-latest` qualify. The action uses the runner's own `python3` in a private virtualenv and doesn't change the Python version your job uses.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```

| Path | Purpose |
|---|---|
| `action.yml` | Composite action definition |
| `src/dep_reviewer/__main__.py` | Orchestration: PR review mode and merge-queue mode |
| `src/dep_reviewer/gate.py` | Verdict rules and merge-result detection |
| `src/dep_reviewer/claude_review.py` | Claude agent loop (tool use, turn limit, refusal and pause handling) |
| `src/dep_reviewer/claude_code_review.py` | The same review through the Claude Code CLI, for OAuth tokens |
| `src/dep_reviewer/test_result.py` | Test result handed from `mode: test` to `mode: review`, and its checks |
| `src/dep_reviewer/mcp_server.py` | Serves the repository tools to Claude Code over MCP (stdio) |
| `src/dep_reviewer/repo_tools.py` | Read-only repository tools exposed to Claude |
| `src/dep_reviewer/prompts.py` | System prompt and PR context message |
| `src/dep_reviewer/schema.py` | Structured-output schema for Claude's review |
| `src/dep_reviewer/report.py` | PR comment rendering |
| `src/dep_reviewer/github_api.py` | Minimal GitHub REST and GraphQL client |
