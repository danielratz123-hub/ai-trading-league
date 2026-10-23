"""Git work copies. The GitHub token is passed per command as an HTTP header,
so it is never written into .git/config where an agent could read it."""
from __future__ import annotations

import base64
import re
import shutil
import subprocess
from pathlib import Path

AUTHOR_EMAIL = "agent-hub@users.noreply.github.com"

# Files no agent change may ever contain, whatever the task says.
FORBIDDEN = (re.compile(r"(^|/)\.env$"), re.compile(r"^data/"), re.compile(r"\.db$"),
             re.compile(r"(^|/)id_[a-z0-9]+$"), re.compile(r"\.pem$"))


class GitError(RuntimeError):
    pass


def _auth(token: str) -> list[str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]


def git(cwd: Path | None, *args: str, token: str | None = None, timeout: int = 600) -> str:
    cmd = ["git", *(_auth(token) if token else []), *args]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()
        if token:
            err = err.replace(token, "***")
        raise GitError(f"git {args[0]} failed: {err[-800:]}")
    return r.stdout


def slug(text: str, n: int = 30) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:n].rstrip("-") or "task")


def branch_name(agent: str, issue: int | None, title: str) -> str:
    return f"hub/{agent}-{issue or 'x'}-{slug(title)}"


class Repo:
    """One task's work copy."""

    def __init__(self, path: Path, repo: str, token: str, base: str, url: str | None = None):
        self.path, self.repo, self.token, self.base = Path(path), repo, token, base
        self.url = url or f"https://github.com/{repo}.git"

    def fresh(self, branch: str) -> None:
        """New copy of the base branch with `branch` checked out."""
        if self.path.exists():
            shutil.rmtree(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        git(None, "clone", "--quiet", "--depth", "50", "--branch", self.base, self.url,
            str(self.path), token=self.token or None)
        git(self.path, "checkout", "-q", "-b", branch)
        self._identity()

    def resume(self, branch: str) -> None:
        """Copy with the task's pushed branch checked out (for fix rounds)."""
        if not (self.path / ".git").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            git(None, "clone", "--quiet", "--depth", "50", "--branch", self.base, self.url,
                str(self.path), token=self.token or None)
        git(self.path, "fetch", "--quiet", "--depth", "50", "origin",
            f"+refs/heads/{self.base}:refs/remotes/origin/{self.base}",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}", token=self.token or None)
        git(self.path, "reset", "-q", "--hard")
        git(self.path, "clean", "-qfd")
        git(self.path, "checkout", "-q", "-B", branch, f"origin/{branch}")
        self._identity()

    def _identity(self) -> None:
        git(self.path, "config", "user.name", "Agent Hub")
        git(self.path, "config", "user.email", AUTHOR_EMAIL)

    def commit_all(self, message: str) -> bool:
        git(self.path, "add", "-A")
        if not git(self.path, "status", "--porcelain").strip():
            return False
        git(self.path, "commit", "-q", "-m", message)
        return True

    def changed_files(self) -> list[str]:
        out = git(self.path, "diff", "--name-only", f"origin/{self.base}...HEAD")
        return [line for line in out.splitlines() if line.strip()]

    def forbidden_files(self) -> list[str]:
        return [f for f in self.changed_files() if any(p.search(f) for p in FORBIDDEN)]

    def diff_text(self) -> str:
        return git(self.path, "diff", f"origin/{self.base}...HEAD")

    def head(self) -> str:
        return git(self.path, "rev-parse", "HEAD").strip()

    def push(self, branch: str) -> None:
        git(self.path, "push", "--quiet", self.url, f"HEAD:refs/heads/{branch}", token=self.token or None)

    def reset_to(self, sha: str) -> None:
        """Throw away anything a reviewer or manager changed."""
        git(self.path, "reset", "-q", "--hard", sha)
        git(self.path, "clean", "-qfd")

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)
