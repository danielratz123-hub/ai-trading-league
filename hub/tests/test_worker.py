from hub.agents import RunResult
from hub.db import DB
from hub.worker import Worker

from .fakes import Agents, Clock, FakeGitHub, FakeRepo, make_cfg


def setup(tmp_path, **cfg_kw):
    cfg = make_cfg(tmp_path, **cfg_kw)
    db = DB(":memory:")
    gh, ag, clock, sent = FakeGitHub(), Agents(), Clock(), []
    repos: dict[int, FakeRepo] = {}

    def factory(t):
        repos.setdefault(t["id"], FakeRepo(t, tmp_path))
        return repos[t["id"]]

    w = Worker(cfg, db, gh, repo_factory=factory, run_agent=ag.run_agent,
               run_checks=ag.run_checks, notify=sent.append, clock=clock)
    return w, db, gh, ag, clock, sent, repos


def start(db, agent="codex"):
    return db.create_task("Add SPY player", "Score SPY like the agents", "SPY on the scoreboard", agent)


def test_happy_path_to_ready(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    w.step()
    t = db.get(tid)
    assert t["status"] == "reviewing"
    assert t["issue"] == 101 and gh.issues[0][2] == ["codex"]
    assert t["branch"] == "hub/codex-101-add-spy-player"
    assert repos[tid].calls[0] == ("fresh", "hub/codex-101-add-spy-player")
    assert ("push", t["branch"]) in repos[tid].calls
    pr = gh.prs[t["pr"]]
    assert pr["base"] == "main" and "Closes #101" in pr["body"] and "Did it" in pr["body"]
    assert "AGENTS.md" in ag.prompts[0] and "Add SPY player" in ag.prompts[0]

    w.step()                                   # CI + review not finished yet
    assert db.get(tid)["status"] == "reviewing"

    gh.review("**Verdict:** ✅ Ready to merge\n**Must fix:** none", at=clock.t + 60)
    w.step()
    assert db.get(tid)["status"] == "ready"
    assert any("ready to merge" in s for s in sent)


def test_needs_fixes_runs_a_fix_round_with_the_feedback(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    w.step()
    gh.review("**Verdict:** 🔧 Needs fixes\n**Must fix:**\n1. Handle missing SPY data", at=clock.t + 30)
    gh.review_comment_list.append({"user": {"login": "claude[bot]"}, "created_at": "2027-01-15T08:01:00Z",
                                   "path": "arena/x.py", "line": 12, "body": "Off by one"})
    w.step()                                    # reads the review, queues round 1, runs it
    t = db.get(tid)
    assert t["round"] == 1
    assert ("resume", t["branch"]) in repos[tid].calls
    fix_prompt = ag.prompts[-1]
    assert "Handle missing SPY data" in fix_prompt and "arena/x.py:12: Off by one" in fix_prompt
    assert t["status"] == "reviewing" and t["feedback"] is None
    assert gh.comments and "round 1" in gh.comments[-1][1]


def test_stops_after_max_rounds(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path, max_rounds=1)
    tid = start(db)
    w.step()
    gh.review("Verdict: 🔧 Needs fixes", at=clock.t + 1)
    w.step()                                    # round 1
    clock.t += 100
    gh.review("Verdict: 🔧 Needs fixes", at=clock.t + 1)
    w.step()
    t = db.get(tid)
    assert t["status"] == "needs_joe" and "1 fix rounds" in t["note"]
    assert any("needs you" in s for s in sent)


def test_stop_verdict_and_ci_failure(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    w.step()
    gh.review("Verdict: ⛔ Stop, Joe must look", at=clock.t + 1)
    w.step()
    assert db.get(tid)["status"] == "needs_joe"

    tid2 = start(db, "gemini")
    w.step()
    gh.issue_comment_list.clear()
    gh.review("Verdict: ✅ Ready to merge", at=clock.t + 1, ci="failure")
    w.step()                                    # ✅ but CI red -> fix round
    assert db.get(tid2)["round"] == 1
    assert "CI ALSO FAILED" in ag.prompts[-1]


def test_forbidden_files_are_never_pushed(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    orig = w.repo_factory

    def factory(t):
        r = orig(t)
        r.files = ["arena/x.py", ".env"]
        return r
    w.repo_factory = factory
    w.step()
    t = db.get(tid)
    assert t["status"] == "needs_joe" and ".env" in t["note"]
    assert not any(c[0] == "push" for c in repos[tid].calls) and not gh.prs


def test_failing_checks_go_back_to_the_agent_twice(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    ag.check_results = [RunResult(False, 1, "1 failed")] * 3
    tid = start(db)
    w.step()
    t = db.get(tid)
    assert len(ag.prompts) == 3                 # build + 2 fix attempts
    assert "CI checks failed" in ag.prompts[1]
    assert t["status"] == "reviewing" and "own test run failed" in t["note"]
    assert "did **not** pass" in gh.prs[t["pr"]]["body"]


def test_agent_error_needs_joe(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    ag.agent_results = [RunResult(False, 1, "not logged in", timed_out=False)]
    tid = start(db)
    w.step()
    assert db.get(tid)["status"] == "needs_joe"
    assert not gh.prs


def test_cancel_closes_the_pr(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    w.step()
    w.cancel(tid)
    t = db.get(tid)
    assert t["status"] == "cancelled" and gh.closed == [t["pr"]]


def test_merge_only_when_ready(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    w.step()
    assert w.merge(tid) is not None and not gh.merged
    gh.review("Verdict: ✅ Ready to merge", at=clock.t + 1)
    w.step()
    assert w.merge(tid) is None
    assert db.get(tid)["status"] == "merged" and gh.merged == [db.get(tid)["pr"]]


def test_merged_or_closed_on_github(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    w.step()
    gh.prs[db.get(tid)["pr"]]["merged"] = True
    w.step()
    assert db.get(tid)["status"] == "merged"


def test_continue_with_instructions(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    w.step()
    gh.review("Verdict: ⛔ Stop", at=clock.t + 1)
    w.step()
    w.resume(tid, "Use the existing benchmark table instead")
    t = db.get(tid)
    assert t["status"] == "queued" and t["round"] == 1
    w.step()
    assert "INSTRUCTIONS FROM JOE" in ag.prompts[-1]


def test_restart_mid_run_needs_joe(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path)
    tid = start(db)
    db.update(tid, status="building")
    w.recover()
    assert db.get(tid)["status"] == "needs_joe"


def test_review_timeout(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path, review_timeout=60)
    tid = start(db)
    w.step()
    clock.t += 61
    w.step()
    assert db.get(tid)["status"] == "needs_joe"


def test_a_secret_in_the_diff_is_never_pushed(tmp_path):
    w, db, gh, ag, clock, sent, repos = setup(tmp_path, token="ghp_very_secret_token_123")
    tid = start(db)
    orig = w.repo_factory

    def factory(t):
        r = orig(t)
        r.diff = "+TOKEN = 'ghp_very_secret_token_123'"
        return r
    w.repo_factory = factory
    w.step()
    assert db.get(tid)["status"] == "needs_joe" and not gh.prs
