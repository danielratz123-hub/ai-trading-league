"""Stand-ins for GitHub, git and the agent CLIs (no network, no subscriptions)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hub.agents import RunResult
from hub.config import Config, DEFAULT_AGENTS


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_cfg(tmp_path: Path, **kw) -> Config:
    base = dict(repo="joe/league", token="tok", data_dir=tmp_path, max_rounds=2,
                agent_timeout=60, check_timeout=60, review_timeout=600, poll_seconds=1,
                agents={k: dict(v) for k, v in DEFAULT_AGENTS.items()})
    base.update(kw)
    return Config(**base)


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeGitHub:
    def __init__(self):
        self.issues, self.prs, self.comments, self.closed, self.merged = [], {}, [], [], []
        self.issue_comment_list: list[dict] = []
        self.review_comment_list: list[dict] = []
        self.runs: list[dict] = []

    def create_issue(self, title, body, labels):
        self.issues.append((title, body, labels))
        return 100 + len(self.issues)

    def create_pr(self, title, head, base, body):
        n = 200 + len(self.prs)
        self.prs[n] = {"title": title, "head": head, "base": base, "body": body,
                       "state": "open", "merged": False, "head_sha": None}
        return n

    def pr(self, n):
        p = self.prs[n]
        return {"state": p["state"], "merged": p["merged"], "head": {"sha": p["head_sha"]}}

    def comment(self, n, body):
        self.comments.append((n, body))

    def issue_comments(self, n):
        return list(self.issue_comment_list)

    def review_comments(self, n):
        return list(self.review_comment_list)

    def merge_pr(self, n, title):
        self.merged.append(n)
        self.prs[n]["merged"] = True

    def close_pr(self, n):
        self.closed.append(n)
        self.prs[n]["state"] = "closed"

    def workflow_runs(self, sha):
        return list(self.runs)

    # helpers for tests
    def review(self, body: str, at: float, ci: str = "success", review: str = "success"):
        self.runs = [{"name": "CI", "status": "completed", "conclusion": ci, "event": "pull_request"},
                     {"name": "Claude review", "status": "completed", "conclusion": review,
                      "event": "pull_request"}]
        self.issue_comment_list.append({"user": {"login": "claude[bot]"}, "created_at": iso(at),
                                        "body": body})


class FakeRepo:
    """Remembers what the worker did; changed files are set by the test."""
    instances: list["FakeRepo"] = []

    def __init__(self, task: dict, tmp: Path):
        self.path = tmp / f"task-{task['id']}"
        self.path.mkdir(parents=True, exist_ok=True)
        self.calls: list[tuple] = []
        self.files = ["arena/x.py"]
        self.sha = 0
        FakeRepo.instances.append(self)

    def fresh(self, branch):
        self.calls.append(("fresh", branch))

    def resume(self, branch):
        self.calls.append(("resume", branch))

    def commit_all(self, msg):
        self.calls.append(("commit", msg))
        return True

    def changed_files(self):
        return list(self.files)

    def forbidden_files(self):
        return [f for f in self.files if f.endswith(".env") or f.startswith("data/")]

    def diff_text(self):
        return getattr(self, "diff", "+ some change")

    def head(self):
        return f"sha{self.sha}"

    def push(self, branch):
        self.sha += 1
        self.calls.append(("push", branch))

    def remove(self):
        self.calls.append(("remove",))


class Agents:
    """Fake run_agent / run_checks with scripted results."""

    def __init__(self):
        self.prompts: list[str] = []
        self.agent_results: list[RunResult] = []
        self.check_results: list[RunResult] = []

    def run_agent(self, agent_cfg, prompt, cwd, log_path, timeout, should_stop):
        self.prompts.append(prompt)
        return self.agent_results.pop(0) if self.agent_results else RunResult(True, 0, "Did it. Tests pass.")

    def run_checks(self, cwd, log_path, timeout, should_stop):
        return self.check_results.pop(0) if self.check_results else RunResult(True, 0, "416 passed")
