"""Stand-ins for GitHub, git and the agent CLIs (no network, no subscriptions)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from hub.agents import RunResult
from hub.config import Config, load_agents


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_cfg(tmp_path: Path, **kw) -> Config:
    base = dict(repo="joe/league", token="tok", data_dir=tmp_path, max_rounds=2,
                agent_timeout=60, check_timeout=60, review_timeout=600, poll_seconds=1,
                agents=load_agents())
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
        return {"state": p["state"], "merged": p["merged"], "head": {"sha": p["head_sha"]},
                "merge_commit_sha": f"merge{n}" if p["merged"] else None}

    def comment(self, n, body):
        self.comments.append((n, body))

    def issue_comments(self, n):
        return list(self.issue_comment_list)

    def review_comments(self, n):
        return list(self.review_comment_list)

    def merge_pr(self, n, title):
        self.merged.append(n)
        self.prs[n]["merged"] = True
        return f"merge{n}"

    def delete_branch(self, branch):
        self.deleted = getattr(self, "deleted", []) + [branch]

    def close_pr(self, n):
        self.closed.append(n)
        self.prs[n]["state"] = "closed"

    def workflow_runs(self, sha):
        return list(self.runs)

    # helper for tests
    def ci(self, conclusion: str = "success"):
        self.runs = [{"name": "CI", "status": "completed", "conclusion": conclusion, "event": "pull_request"}]


class FakeRepo:
    """Remembers what the worker did; changed files are set by the test."""
    instances: list["FakeRepo"] = []

    def __init__(self, task: dict, tmp: Path):
        self.path = tmp / (f"plan-{task['plan']}" if "plan" in task else f"task-{task['id']}")
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
        return getattr(self, "head_override", None) or f"sha{self.sha}"

    def push(self, branch):
        self.sha += 1
        self.calls.append(("push", branch))

    def reset_to(self, sha):
        self.calls.append(("reset", sha))

    def remove(self):
        self.calls.append(("remove",))


PLAN_OUTPUT = """Looked at arena/league.py.
BEGIN_PLAN
{"summary": "Two steps.", "tasks": [
 {"title": "SPY player", "details": "Add it in arena/league.py", "done_when": "tests", "builder": "codex", "why": "fast"},
 {"title": "Show SPY on the scoreboard", "details": "arena/web", "done_when": "visible", "builder": "nobody"}]}
END_PLAN
"""


class Agents:
    """Fake run_agent / run_checks with scripted results. Builders, reviewers
    and managers are told apart by their prompt; each run is also written to
    log_path, like the real runner does."""

    def __init__(self):
        self.prompts: list[str] = []
        self.calls: list[tuple[str, str]] = []           # (role, agent command[0])
        self.agent_results: list[RunResult] = []
        self.check_results: list[RunResult] = []
        self.reviews: list[str] = []
        self.plan_output = PLAN_OUTPUT

    def run_agent(self, agent_cfg, prompt, cwd, log_path, timeout, should_stop):
        self.prompts.append(prompt)
        self.cfgs = getattr(self, "cfgs", []) + [dict(agent_cfg)]
        role = "review" if "the REVIEWER" in prompt else "manager" if "the MANAGER" in prompt else "build"
        cli = agent_cfg["cmd"][0]
        self.calls.append((role, {"agy": "gemini"}.get(cli, cli)))     # agy = Gemini's CLI
        if role == "review":
            out = self.reviews.pop(0) if self.reviews else "Looks fine.\n**Verdict:** ✅ Ready to merge\n**Must fix:** none"
            result = RunResult(True, 0, out)
        elif role == "manager":
            out = self.plan_output
            result = RunResult(True, 0, out)
        else:
            result = self.agent_results.pop(0) if self.agent_results else RunResult(True, 0, "Did it. Tests pass.")
            out = result.tail
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(f"$ cmd prompt...\n{out}")
        return result

    def run_checks(self, cwd, log_path, timeout, should_stop):
        return self.check_results.pop(0) if self.check_results else RunResult(True, 0, "416 passed")
