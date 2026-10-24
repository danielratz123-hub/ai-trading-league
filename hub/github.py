"""The few GitHub REST calls the hub needs (one repo, fine-grained token)."""
from __future__ import annotations

from datetime import datetime

import httpx

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


def parse_time(value: str | None) -> float:
    """GitHub's '2026-10-23T14:20:44Z' -> unix seconds (0 if missing)."""
    if not value:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class GitHub:
    def __init__(self, repo: str, token: str, client: httpx.Client | None = None):
        self.repo = repo
        self.client = client or httpx.Client(
            base_url=API, timeout=30,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "agent-hub"})

    def _call(self, method: str, path: str, **kw):
        r = self.client.request(method, f"/repos/{self.repo}{path}", **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", "")
            except ValueError:
                msg = r.text[:200]
            raise GitHubError(f"GitHub {method} {path}: HTTP {r.status_code} {msg}")
        return r.json() if r.content else {}

    # Issues and PRs -------------------------------------------------------

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        try:
            return int(self._call("POST", "/issues", json={"title": title, "body": body,
                                                           "labels": labels})["number"])
        except GitHubError:
            # A missing label is not worth failing the task over.
            return int(self._call("POST", "/issues", json={"title": title, "body": body})["number"])

    def create_pr(self, title: str, head: str, base: str, body: str) -> int:
        return int(self._call("POST", "/pulls", json={"title": title, "head": head, "base": base,
                                                     "body": body})["number"])

    def pr(self, number: int) -> dict:
        return self._call("GET", f"/pulls/{number}")

    def comment(self, number: int, body: str) -> None:
        self._call("POST", f"/issues/{number}/comments", json={"body": body})

    def issue_comments(self, number: int) -> list[dict]:
        return self._call("GET", f"/issues/{number}/comments", params={"per_page": 100})

    def review_comments(self, number: int) -> list[dict]:
        return self._call("GET", f"/pulls/{number}/comments", params={"per_page": 100})

    def merge_pr(self, number: int, title: str) -> str:
        """Merge; returns the merge commit's sha (what the deploy runs on)."""
        data = self._call("PUT", f"/pulls/{number}/merge",
                          json={"merge_method": "merge", "commit_title": title})
        return str(data.get("sha") or "")

    def delete_branch(self, branch: str) -> None:
        """Only the hub's own branches. Already gone is fine."""
        if not branch.startswith("hub/"):
            return
        try:
            self._call("DELETE", f"/git/refs/heads/{branch}")
        except GitHubError as e:
            if "422" not in str(e) and "404" not in str(e):
                raise

    def pr_files(self, number: int) -> list[str]:
        files: list[str] = []
        for page in range(1, 11):
            batch = self._call("GET", f"/pulls/{number}/files", params={"per_page": 100, "page": page})
            files += [f.get("filename", "") for f in batch]
            if len(batch) < 100:
                break
        return files

    def close_pr(self, number: int) -> None:
        self._call("PATCH", f"/pulls/{number}", json={"state": "closed"})

    # Checks ----------------------------------------------------------------

    def workflow_runs(self, head_sha: str) -> list[dict]:
        """Workflow runs for one commit: [{'name', 'status', 'conclusion', 'event'}]."""
        data = self._call("GET", "/actions/runs", params={"head_sha": head_sha, "per_page": 50})
        return [{"name": r.get("name", ""), "status": r.get("status", ""),
                 "conclusion": r.get("conclusion"), "event": r.get("event", ""),
                 "url": r.get("html_url", "")}
                for r in data.get("workflow_runs", [])]
