"""The hub's brain: one task at a time, from task to "ready to merge".

    queued ──► building ──► reviewing ──► ready ──(Joe merges)──► merged
                  ▲             │
                  └── queued ◄──┘  Claude's review said "Needs fixes"
                     (fix round)    (up to max_rounds, then needs_joe)

Agents never run in parallel: they share Joe's subscriptions and a small VPS.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from . import agents, prompts, review
from .config import Config
from .db import DB, RUNNING
from .gitops import Repo, branch_name

log = logging.getLogger("hub")


class Worker:
    def __init__(self, cfg: Config, db: DB, gh, *,
                 repo_factory: Callable[[dict], Repo] | None = None,
                 run_agent=agents.run_agent, run_checks=agents.run_checks,
                 notify: Callable[[str], None] = lambda text: None,
                 clock: Callable[[], float] = time.time):
        self.cfg, self.db, self.gh = cfg, db, gh
        self.repo_factory = repo_factory or (lambda t: Repo(
            cfg.work_dir / f"task-{t['id']}", cfg.repo, cfg.token, cfg.base_branch))
        self.run_agent, self.run_checks = run_agent, run_checks
        self.notify, self.clock = notify, clock
        self.wake = threading.Event()
        self.lock = threading.Lock()     # one state change at a time (worker vs. web)

    # ------------------------------------------------------------------ loop

    def recover(self) -> None:
        """After a restart, a task that was mid-run can't be resumed safely."""
        for t in self.db.with_status(*RUNNING):
            self._stop(t, "needs_joe", "The hub restarted while the agent was working. "
                                       "Press Continue to run it again.")

    def forever(self) -> None:
        self.recover()
        while True:
            try:
                self.step()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.error("hub step failed:\n%s", traceback.format_exc())
            self.wake.wait(self.cfg.poll_seconds)
            self.wake.clear()

    def step(self) -> None:
        for t in self.db.with_status("reviewing", "ready"):
            try:
                self.poll(t)
            except Exception as e:  # noqa: BLE001 - GitHub hiccups: try again next pass
                log.warning("poll of task %s failed: %s", t["id"], e)
        queued = self.db.with_status("queued")
        if queued:
            self.run(queued[0])

    # ------------------------------------------------------------- running

    def _label(self, t: dict) -> str:
        return self.cfg.agents[t["agent"]]["label"]

    def _stopping(self, task_id: int) -> Callable[[], bool]:
        return lambda: bool((self.db.get(task_id) or {}).get("cancel"))

    def _stop(self, t: dict, status: str, note: str, tell: bool = True) -> None:
        self.db.update(t["id"], status=status, note=note)
        self.db.event(t["id"], status, note)
        if tell and status in ("needs_joe", "failed"):
            self.notify(f"⚠️ Task #{t['id']} ({t['title']}) needs you: {note}")

    def run(self, t: dict) -> None:
        tid, fixing = t["id"], bool(t["pr"])
        self.db.update(tid, status="fixing" if fixing else "building", note=None)
        self.db.event(tid, "start", f"{self._label(t)} {'fix round ' + str(t['round']) if fixing else 'starts building'}")
        repo = self.repo_factory(t)
        try:
            if not fixing:
                if not t["issue"]:
                    issue = self.gh.create_issue(f"[task] {t['title']}", self._issue_body(t), [t["agent"]])
                    self.db.update(tid, issue=issue)
                    self.db.event(tid, "issue", f"Issue #{issue} created")
                t = self.db.get(tid)
                branch = t["branch"] or branch_name(t["agent"], t["issue"], t["title"])
                self.db.update(tid, branch=branch)
                t = self.db.get(tid)
                repo.fresh(branch)
                prompt = prompts.build(self._label(t), t)
            else:
                repo.resume(t["branch"])
                prompt = prompts.fix(self._label(t), t, t["feedback"] or "", self.cfg.max_rounds)

            logs = self.cfg.log_dir
            result = self.run_agent(self.cfg.agents[t["agent"]], prompt, repo.path,
                                    logs / f"task-{tid}-r{t['round']}.log",
                                    self.cfg.agent_timeout, self._stopping(tid))
            if result.stopped:
                return self._cancelled(t)
            self.db.event(tid, "agent", result.tail[-4000:])
            if not result.ok:
                why = "ran out of time" if result.timed_out else f"stopped with an error (exit {result.exit_code})"
                return self._stop(t, "needs_joe", f"{self._label(t)} {why}. See the log below.")
            summary = result.tail
            repo.commit_all(f"{self._label(t)}: {t['title']}" if not fixing
                            else f"{self._label(t)}: review fixes (round {t['round']})")

            checks = self._checks(t, repo)
            if checks.stopped:
                return self._cancelled(t)

            bad = repo.forbidden_files()
            if bad:
                return self._stop(t, "needs_joe", "The change includes files that must never be "
                                  f"committed ({', '.join(bad[:5])}). Nothing was pushed.")
            diff = repo.diff_text()
            if any(secret in diff for secret in self.cfg.secrets):
                return self._stop(t, "needs_joe", "The change contains one of the hub's secrets "
                                  "(a token). Nothing was pushed. Check the work copy on the server.")
            if not repo.changed_files():
                return self._stop(t, "needs_joe", f"{self._label(t)} finished without changing anything.")

            repo.push(t["branch"])
            head = repo.head()
            now = self.clock()
            if not fixing:
                pr = self.gh.create_pr(f"[{t['agent']}] {t['title']}", t["branch"],
                                       self.cfg.base_branch,
                                       prompts.pr_body(t, self._label(t), summary, checks.ok))
                self.db.update(tid, pr=pr)
                self.db.event(tid, "pr", f"PR #{pr} opened")
            else:
                self.gh.comment(t["pr"], f"🧰 Agent Hub: {self._label(t)} pushed fixes "
                                         f"(round {t['round']}).\n\n{summary.strip()[-3000:]}")
                self.db.event(tid, "pushed", f"Fixes pushed (round {t['round']})")
            note = None if checks.ok else "The hub's own test run failed; waiting for CI and review anyway."
            self.db.update(tid, status="reviewing", head_sha=head, pushed_at=now, feedback=None, note=note)
            self.db.event(tid, "reviewing", "Waiting for CI and Claude's review")
        except Exception as e:  # noqa: BLE001 - surface it on the page, keep the hub alive
            log.error("task %s failed:\n%s", tid, traceback.format_exc())
            self._stop(self.db.get(tid), "failed", f"{type(e).__name__}: {e}"[:1500])

    def _checks(self, t: dict, repo: Repo):
        tid = t["id"]
        log_path = self.cfg.log_dir / f"task-{tid}-r{t['round']}-checks.log"
        checks = self.run_checks(repo.path, log_path, self.cfg.check_timeout, self._stopping(tid))
        tries = 0
        while not checks.ok and not checks.stopped and tries < 2:
            tries += 1
            self.db.event(tid, "checks", f"Checks failed, asking {self._label(t)} to fix them "
                                         f"(try {tries} of 2)\n{checks.tail[-2000:]}")
            fix = self.run_agent(self.cfg.agents[t["agent"]], prompts.checks_failed(checks.tail),
                                 repo.path, self.cfg.log_dir / f"task-{tid}-r{t['round']}-fix{tries}.log",
                                 self.cfg.agent_timeout, self._stopping(tid))
            if fix.stopped:
                checks.stopped = True
                return checks
            repo.commit_all(f"{self._label(t)}: fix failing checks")
            checks = self.run_checks(repo.path, log_path, self.cfg.check_timeout, self._stopping(tid))
        self.db.event(tid, "checks", "Checks pass ✅" if checks.ok else "Checks still failing")
        return checks

    def _issue_body(self, t: dict) -> str:
        return (f"### Who builds it\n{t['agent']}\n\n### What to build\n{t['body'] or t['title']}\n\n"
                f"### Done when\n{t['done_when'] or '-'}\n\n_Started from the Agent Hub (task #{t['id']})._")

    def _cancelled(self, t: dict) -> None:
        t = self.db.get(t["id"])
        if t["pr"]:
            try:
                self.gh.comment(t["pr"], "🧰 Agent Hub: task cancelled by Joe.")
                self.gh.close_pr(t["pr"])
            except Exception as e:  # noqa: BLE001
                log.warning("could not close PR #%s: %s", t["pr"], e)
        self._stop(t, "cancelled", "Cancelled", tell=False)
        self.repo_factory(t).remove()

    # ------------------------------------------------------------- polling

    def poll(self, t: dict) -> None:
        tid = t["id"]
        pr = self.gh.pr(t["pr"])
        if pr.get("merged"):
            self.db.update(tid, status="merged", note=None)
            self.db.event(tid, "merged", f"PR #{t['pr']} merged")
            self.repo_factory(t).remove()
            return
        if pr.get("state") == "closed":
            self._stop(t, "cancelled", f"PR #{t['pr']} was closed", tell=False)
            return
        if t["status"] == "ready":
            return
        head = (pr.get("head") or {}).get("sha")
        if head and t["head_sha"] and head != t["head_sha"]:
            # Someone (Joe) pushed to the branch: follow the new commit.
            self.db.update(tid, head_sha=head, pushed_at=self.clock())
            self.db.event(tid, "pushed", "New commit on the branch, waiting for its review")
            return

        state = review.checks_state(self.gh.workflow_runs(t["head_sha"]))
        waited = self.clock() - (t["pushed_at"] or 0)
        if not state.done:
            if waited > self.cfg.review_timeout:
                self._stop(t, "needs_joe", f"No CI result or review after {self.cfg.review_timeout // 60} min.")
            return
        if state.review != "success":
            return self._stop(t, "needs_joe", "Claude's review didn't run properly "
                                              f"({state.review}). Check the Claude review workflow.")
        summary = review.latest_summary(self.gh.issue_comments(t["pr"]), t["pushed_at"] or 0)
        if summary is None:
            if waited > self.cfg.review_timeout:
                self._stop(t, "needs_joe", "The review finished but posted no verdict.")
            return

        verdict = review.verdict(summary["body"])
        self.db.event(tid, "review", summary["body"][:6000])
        if verdict == review.OK and state.ci == "success":
            self.db.update(tid, status="ready", note=None)
            self.db.event(tid, "ready", "✅ Ready to merge")
            self.notify(f"✅ Task #{tid} ({t['title']}) is ready to merge: PR #{t['pr']}.")
            return
        if verdict in (review.STOP, review.UNKNOWN):
            return self._stop(t, "needs_joe", "Claude's review says Joe must look (⛔)."
                              if verdict == review.STOP else "Couldn't read the review's verdict.")
        if t["round"] >= self.cfg.max_rounds:
            return self._stop(t, "needs_joe", f"Still not ready after {t['round']} fix rounds.")
        feedback = "REVIEW SUMMARY:\n" + summary["body"].strip()
        inline = review.inline_since(self.gh.review_comments(t["pr"]), t["pushed_at"] or 0)
        if inline:
            feedback += "\n\nINLINE COMMENTS:\n" + "\n".join(inline)
        if state.ci != "success":
            feedback += ("\n\nCI ALSO FAILED on GitHub. Run the checks from AGENTS.md and make them pass.")
        self.db.update(tid, status="queued", round=t["round"] + 1, feedback=feedback)
        self.db.event(tid, "fix", f"Review asked for fixes: starting round {t['round'] + 1}")
        self.wake.set()

    # ------------------------------------------------------ Joe's buttons

    def cancel(self, task_id: int) -> None:
        with self.lock:
            t = self.db.get(task_id)
            if not t or t["status"] in ("merged", "cancelled"):
                return
            self.db.update(task_id, cancel=1)
            if t["status"] not in RUNNING:     # running: the worker stops it within a second
                self._cancelled(t)

    def resume(self, task_id: int, instructions: str = "") -> None:
        """Continue a stopped task, optionally with Joe's instructions."""
        with self.lock:
            t = self.db.get(task_id)
            if not t or t["status"] in RUNNING + ("merged", "cancelled", "queued"):
                return
            text = instructions.strip()
            if t["pr"]:
                feedback = f"INSTRUCTIONS FROM JOE:\n{text}" if text else (t["feedback"] or
                           "Look at the PR's review comments and CI result and fix what's needed.")
                self.db.update(task_id, status="queued", round=t["round"] + 1, feedback=feedback,
                               cancel=0, note=None)
            else:
                body = t["body"] + (f"\n\nMore instructions from Joe:\n{text}" if text else "")
                self.db.update(task_id, status="queued", body=body, cancel=0, note=None)
            self.db.event(task_id, "continue", text or "Continue")
        self.wake.set()

    def merge(self, task_id: int) -> str | None:
        """Merge a ready PR. Returns an error message, or None."""
        with self.lock:
            t = self.db.get(task_id)
            if not t or t["status"] != "ready":
                return "Only a task marked ready can be merged here."
            self.gh.merge_pr(t["pr"], f"[{t['agent']}] {t['title']} (#{t['pr']})")
            self.db.update(task_id, status="merged", note=None)
            self.db.event(task_id, "merged", f"PR #{t['pr']} merged from the hub; deploying")
            self.repo_factory(t).remove()
            return None


def log_text(cfg: Config, task: dict, limit: int = 20000) -> str:
    """The newest log of a task, for the page."""
    logs = sorted(Path(cfg.log_dir).glob(f"task-{task['id']}-*.log"),
                  key=lambda p: p.stat().st_mtime)
    return agents.tail(logs[-1], limit) if logs else ""
