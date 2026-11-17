"""Minimal GitHub REST client (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

PER_PAGE = 100
# GitHub's pull request files endpoint returns at most 3000 files.
MAX_FILE_PAGES = 30


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


class GitHub:
    def __init__(self, token: str, api_url: str, repository: str, graphql_url: str | None = None):
        self._token = token
        self._api = api_url.rstrip("/")
        self._graphql = graphql_url or f"{self._api}/graphql"
        self.repository = repository

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        url = path if path.startswith("http") else f"{self._api}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "claude-dependabot-reviewer",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise GitHubError(e.code, detail) from None
        return json.loads(raw) if raw else None

    def _repo(self, suffix: str) -> str:
        return f"/repos/{self.repository}{suffix}"

    def get_pr(self, number: int) -> dict:
        return self._request("GET", self._repo(f"/pulls/{number}"))

    def list_pr_files(self, number: int) -> list[dict]:
        files: list[dict] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            batch = self._request("GET", self._repo(f"/pulls/{number}/files?per_page={PER_PAGE}&page={page}"))
            files.extend(batch)
            if len(batch) < PER_PAGE:
                break
        return files

    def _list_comments(self, number: int) -> list[dict]:
        comments: list[dict] = []
        page = 1
        while True:
            batch = self._request("GET", self._repo(f"/issues/{number}/comments?per_page={PER_PAGE}&page={page}"))
            comments.extend(batch)
            if len(batch) < PER_PAGE:
                return comments
            page += 1

    def find_comment(self, number: int, marker: str) -> dict | None:
        """The first comment containing `marker`: the one upsert_comment updates."""
        for c in self._list_comments(number):
            if marker in (c.get("body") or ""):
                return c
        return None

    def upsert_comment(self, number: int, body: str, marker: str) -> str:
        """Update the comment containing `marker`, or create one. Returns its URL."""
        existing = self.find_comment(number, marker)
        if existing is not None:
            updated = self._request("PATCH", self._repo(f"/issues/comments/{existing['id']}"), {"body": body})
            return updated["html_url"]
        created = self._request("POST", self._repo(f"/issues/{number}/comments"), {"body": body})
        return created["html_url"]

    def ensure_label(self, name: str, color: str, description: str) -> None:
        try:
            self._request("POST", self._repo("/labels"), {"name": name, "color": color, "description": description})
        except GitHubError as e:
            if e.status != 422:  # 422 = label already exists
                raise

    def set_exclusive_label(self, number: int, current: list[str], prefix: str, label: str) -> None:
        """Add `label` and remove any other label on the PR that starts with `prefix`."""
        for name in current:
            if name.startswith(prefix) and name != label:
                try:
                    self._request("DELETE", self._repo(f"/issues/{number}/labels/{urllib.parse.quote(name, safe='')}"))
                except GitHubError as e:
                    if e.status != 404:
                        raise
        if label not in current:
            self._request("POST", self._repo(f"/issues/{number}/labels"), {"labels": [label]})

    def _graphql_mutation(self, query: str, variables: dict) -> Any:
        result = self._request("POST", self._graphql, {"query": query, "variables": variables})
        if result.get("errors"):
            raise GitHubError(200, "; ".join(e.get("message", "") for e in result["errors"]))
        return result["data"]

    def enable_auto_merge(self, pr_node_id: str, method: str) -> None:
        """Merge once all required checks pass. Fails if the PR has no pending requirements."""
        self._graphql_mutation(
            "mutation($id: ID!, $method: PullRequestMergeMethod!) {"
            " enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) { clientMutationId } }",
            {"id": pr_node_id, "method": method.upper()},
        )

    def other_checks(self, sha: str, own_run_id: str | None) -> tuple[list[str], list[str]]:
        """Checks on `sha` other than this workflow run: (still running, failed) names.

        Covers check runs (GitHub Actions and apps) and legacy commit statuses.
        """
        pending: list[str] = []
        failed: list[str] = []
        page = 1
        while True:
            data = self._request("GET", self._repo(f"/commits/{sha}/check-runs?per_page={PER_PAGE}&page={page}"))
            runs = data.get("check_runs", [])
            for run in runs:
                if own_run_id and f"/actions/runs/{own_run_id}/" in (run.get("details_url") or ""):
                    continue
                if run["status"] != "completed":
                    pending.append(run["name"])
                elif run.get("conclusion") not in ("success", "neutral", "skipped"):
                    failed.append(f"{run['name']} ({run.get('conclusion')})")
            if len(runs) < PER_PAGE:
                break
            page += 1
        for status in self._request("GET", self._repo(f"/commits/{sha}/status")).get("statuses", []):
            if status["state"] == "pending":
                pending.append(status["context"])
            elif status["state"] != "success":
                failed.append(f"{status['context']} ({status['state']})")
        return pending, failed

    def get_commit(self, sha: str) -> dict:
        return self._request("GET", self._repo(f"/commits/{sha}"))

    def get_commit_message(self, sha: str) -> str:
        return self.get_commit(sha)["commit"]["message"]

    def merge_pr(self, number: int, head_sha: str, method: str) -> None:
        """Merge now. GitHub refuses (409) if the head is no longer `head_sha`."""
        self._request("PUT", self._repo(f"/pulls/{number}/merge"), {"sha": head_sha, "merge_method": method})

    def disable_auto_merge(self, pr_node_id: str) -> None:
        self._graphql_mutation(
            "mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) { clientMutationId } }",
            {"id": pr_node_id},
        )
