"""Phase 3: the weekly league loop, end to end, with scripted agents."""
import json
import math

import pytest

from arena import league, ledger, scoring
from arena.agents import briefing
from arena.agents.base import run_turn
from arena.db import dry_run_guard
from arena.league import LeagueError
from arena.tools.context import ResearchContext
from arena.tools.registry import Budget, ToolBox

from .fakes import Factory, ScriptedAgent, manual, submit
from .test_tools import _db

START = "2024-01-02"
WEEK1, WEEK2, WEEK3 = "2024-01-05", "2024-01-12", "2024-01-19"   # Friday decision dates


@pytest.fixture
def conn():
    c = _db()
    yield c
    c.close()


def cash_only():
    return [[submit([])]]


def week_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM weeks ORDER BY week_no")]


def run_weeks(conn, factory, n):
    league.simulate(conn, START, n, factory=factory, progress=lambda _: None)


# ---------------------------------------------------------------- full loop


def test_full_loop_scores_weeks_and_awards_points(conn):
    f = Factory({
        "claude": [[[("market_overview", {})], [submit([manual(1, 0.5, {"SPY": 0.5, "AAPL": 0.5})])]]],
        "gpt": [cash_only()],
    })
    run_weeks(conn, f, 3)
    weeks = week_rows(conn)
    assert [w["status"] for w in weeks] == ["scored"] * 3
    assert weeks[0]["exec_date"] == "2024-01-08"            # Monday open after Friday decision

    scores = {(r["week_no"], r["agent_id"]): dict(r) for r in conn.execute("SELECT * FROM scores")}
    for wk in (1, 2, 3):
        c, g = scores[(wk, "claude")], scores[(wk, "gpt")]
        assert g["weekly_return"] == pytest.approx(0.0, abs=1e-12)   # all cash -> exactly flat
        # The higher return gets the point; ties get nobody.
        assert c["point"] == int(c["weekly_return"] > 0)
        assert g["point"] == int(c["weekly_return"] < 0)
        # Return definition: end (mark at next exec open) / start (post_trade) - 1
        assert c["weekly_return"] == pytest.approx(c["end_equity"] / c["start_equity"] - 1)

    # Every turn was logged with a transcript
    turns = conn.execute("SELECT * FROM agent_turns").fetchall()
    assert len(turns) == 6 and all(t["status"] == "accepted" for t in turns)
    assert conn.execute("SELECT COUNT(*) FROM turn_messages").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM tool_calls WHERE agent_id='claude'").fetchone()[0] == 3


def test_execute_runs_each_decided_week_once(conn):
    f = Factory({"claude": [cash_only()], "gpt": [cash_only()]})
    league.decide(conn, WEEK1, factory=f)
    league.execute(conn)
    with pytest.raises(LeagueError, match="Nothing to execute"):
        league.execute(conn)
    league.daily(conn, "2024-01-08")
    with pytest.raises(LeagueError, match="already executed"):
        league.decide(conn, WEEK1, factory=f)


def test_trades_happen_at_next_open_not_at_decision(conn):
    f = Factory({"claude": [[[submit([manual(1, 1.0, {"MSFT": 0.25})])]]], "gpt": [cash_only()]})
    league.decide(conn, WEEK1, factory=f)
    assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0   # only pending
    league.execute(conn)
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["date"] == "2024-01-08"
    open_px = conn.execute("SELECT open FROM prices WHERE ticker='MSFT' AND date='2024-01-08'").fetchone()[0]
    assert t["ref_price"] == pytest.approx(open_px)


def test_bonus_every_four_weeks_is_a_deposit_not_a_return(conn):
    f = Factory({"claude": [[[submit([manual(1, 1.0, {"SPY": 0.25, "QQQ": 0.25, "DIA": 0.25, "IWM": 0.25})])]]],
                 "gpt": [cash_only()]})
    run_weeks(conn, f, 5)
    bonus = conn.execute("SELECT * FROM bonuses").fetchone()
    assert bonus["period_end_week"] == 4
    period = json.loads(bonus["returns_json"])
    expected = "claude" if period["claude"] > period["gpt"] else "gpt"
    assert bonus["winner"] == expected
    dep = conn.execute("SELECT * FROM deposits WHERE note LIKE 'bonus%'").fetchone()
    assert dep["agent_id"] == expected and dep["amount"] == 1000
    # The deposit happened at week 5's exec date, after the mark and before trades:
    exec5 = week_rows(conn)[4]["exec_date"]
    assert dep["date"] == exec5
    # ...so week 5's return for gpt (all cash) is still exactly zero.
    w5 = conn.execute("SELECT * FROM scores WHERE week_no=5 AND agent_id='gpt'").fetchone()
    assert w5["weekly_return"] == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------- validation & failures


def test_invalid_decision_gets_errors_then_fixes_it(conn):
    bad = submit([manual(1, 1.0, {"NVDA": 0.5, "SPY": 0.5})])
    good = submit([manual(1, 1.0, {"NVDA": 0.25, "SPY": 0.25})])
    f = Factory({"claude": [[[bad], [good]]], "gpt": [cash_only()]})
    reports = league.decide(conn, WEEK1, factory=f)
    claude = f.made["claude"][0]
    (name, msg, is_err), = claude.received[0]
    assert name == "submit_decision" and is_err
    assert "NVDA: net long 50.0%" in msg and "gross 100.0%" in msg and "2 retries left" in msg
    r = next(r for r in reports if r.agent_id == "claude")
    assert r.status == "accepted" and r.attempts == 2
    assert conn.execute("SELECT COUNT(*) FROM decision_attempts").fetchone()[0] == 3  # claude 2 + gpt 1


def test_failed_decision_leaves_portfolio_unchanged(conn):
    bad = submit([manual(1, 1.0, {"TSLA": -0.5})])
    f = Factory({"claude": [[[bad], [bad], [bad]]], "gpt": [cash_only()]})
    league.decide(conn, WEEK1, factory=f)
    turn = conn.execute("SELECT * FROM agent_turns WHERE agent_id='claude'").fetchone()
    assert turn["status"] == "failed" and "after all retries" in turn["error"]
    league.execute(conn)
    assert conn.execute("SELECT COUNT(*) FROM trades WHERE agent_id='claude'").fetchone()[0] == 0
    assert ledger.value_agent(conn, "claude", "2024-01-08", "open").equity == pytest.approx(1000)


def test_agent_that_never_submits_is_nudged_then_fails(conn):
    f = Factory({"claude": [["thinking...", "still thinking", "hmm"]], "gpt": [cash_only()]})
    league.decide(conn, WEEK1, factory=f)
    claude = f.made["claude"][0]
    assert sum("submit_decision" in m for m in claude.received if isinstance(m, str)) == 2
    turn = conn.execute("SELECT * FROM agent_turns WHERE agent_id='claude'").fetchone()
    assert turn["status"] == "failed" and "without calling submit_decision" in turn["error"]


def test_research_budget_is_enforced(conn):
    ctx = ResearchContext(conn, "claude", WEEK1)
    ledger.create_agent(conn, "claude", 1000, WEEK1)
    agent = ScriptedAgent("claude", [[("market_overview", {})]] * 5 + [[submit([])]])
    box = ToolBox(ctx, Budget(max_calls=3, max_spend_usd=100))
    from arena.agents.decision import check
    out = run_turn(agent, box, "sys", "brief", lambda a, n: check(ctx, a), lambda *_: None)
    texts = [r[0][1] for r in agent.received if isinstance(r, list)]
    assert sum("budget exhausted" in t.lower() for t in texts) >= 1
    assert out.tool_calls == 5 and box.budget.calls == 3
    ctx.close_session()


def test_api_spend_hard_limit_stops_the_turn(conn):
    from arena.agents.base import Usage
    ctx = ResearchContext(conn, "claude", WEEK1)
    ledger.create_agent(conn, "claude", 1000, WEEK1)
    pricey = ScriptedAgent("claude", [[("market_overview", {})]] * 50, usage=Usage(output=100_000))  # $2/step
    out = run_turn(pricey, ToolBox(ctx, Budget(max_calls=40, max_spend_usd=1.0)), "s", "b",
                   lambda a, n: None, lambda *_: None)
    assert out.accepted is None and out.cost_usd < 1.5 + 2.01
    assert "hard limit" in out.error or "budget exhausted" in out.error.lower()
    ctx.close_session()


def test_web_searches_count_against_the_tool_budget(conn):
    ctx = ResearchContext(conn, "claude", WEEK1)
    ledger.create_agent(conn, "claude", 1000, WEEK1)
    agent = ScriptedAgent("claude", [[("market_overview", {})]], web_searches=3)
    box = ToolBox(ctx, Budget(max_calls=40, max_spend_usd=10))
    out = run_turn(agent, box, "s", "b", lambda a, n: None, lambda *_: None)
    assert out.web_searches >= 3 and box.budget.calls >= 4
    ctx.close_session()


# ---------------------------------------------------------------- code slots


CODE = "def target_weights(prices, date):\n    return {'SPY': 0.5, 'XOM': -0.1}\n"


def test_code_slot_runs_saved_code(conn):
    f = Factory({"claude": [[[("save_strategy_code", {"slot": 2, "code": CODE})],
                             [submit([{"slot": 2, "name": "algo", "type": "code", "allocation": 0.4,
                                       "reason": "r"}])]]],
                 "gpt": [cash_only()]})
    league.decide(conn, WEEK1, factory=f)
    league.execute(conn)
    pos = {p["ticker"]: p["shares"] for p in ledger.positions(conn, "claude")}
    assert pos["SPY"] > 0 and pos["XOM"] < 0
    run = conn.execute("SELECT * FROM slot_runs").fetchone()
    assert run["ok"] == 1 and json.loads(run["weights_json"]) == {"SPY": 0.5, "XOM": -0.1}


def test_crashing_code_slot_is_a_validation_error_the_agent_must_fix(conn):
    crash = "def target_weights(prices, date):\n    raise ValueError('boom')\n"
    code_slot = {"slot": 2, "name": "algo", "type": "code", "allocation": 0.4, "reason": "r"}
    # save_strategy_code test-runs code, so plant the broken version directly
    # (e.g. code that worked last week but fails on new data).
    ledger.create_agent(conn, "claude", 1000, WEEK1)
    conn.execute("INSERT INTO strategy_code(agent_id, slot, saved_date, code) VALUES ('claude', 2, ?, ?)",
                 (WEEK1, crash))
    f = Factory({"claude": [[[submit([code_slot])],
                             [("save_strategy_code", {"slot": 2, "code": CODE})],
                             [submit([code_slot])]]],
                 "gpt": [cash_only()]})
    league.decide(conn, WEEK1, factory=f)
    first_reply = f.made["claude"][0].received[0][0]
    assert first_reply[2] and "REJECTED" in first_reply[1] and "boom" in first_reply[1]
    turn = conn.execute("SELECT * FROM agent_turns WHERE agent_id='claude'").fetchone()
    assert turn["status"] == "accepted" and turn["attempts"] == 2
    league.execute(conn)
    assert {p["ticker"] for p in ledger.positions(conn, "claude")} == {"SPY", "XOM"}


# ---------------------------------------------------------------- fairness


def test_briefing_shows_opponent_returns_but_not_positions_or_notes(conn):
    f = Factory({"claude": [[[submit([manual(1, 0.5, {"SPY": 0.5})], notes="claude plan")]]],
                 "gpt": [[[submit([manual(1, 0.5, {"XOM": 0.5, "GLD": 0.3})], view="gpt view XYZZY",
                                   notes="secret gpt plan")]]]})
    run_weeks(conn, f, 1)
    text = briefing.build(conn, "claude", 2, WEEK2, "2024-01-16", False, 40, 1.0)
    assert "| gpt |" in text and "| claude (you) |" in text           # scoreboard has both
    for leak in ("XOM", "GLD", "secret gpt plan", "XYZZY"):
        assert leak not in text
    assert "claude plan" in text and "SPY" in text                     # own memory is there


def test_both_agents_get_identical_tools_and_prompt_structure(conn):
    f = Factory({"claude": [cash_only()], "gpt": [cash_only()]})
    league.decide(conn, WEEK1, factory=f)
    c, g = f.made["claude"][0], f.made["gpt"][0]
    assert c.tools == g.tools
    assert c.system.replace('"claude"', "X") == g.system.replace('"gpt"', "X")
    assert c.web_search and g.web_search


def test_simulate_turns_off_web_search(conn):
    f = Factory({"claude": [cash_only()], "gpt": [cash_only()]})
    league.decide(conn, WEEK1, simulate=True, factory=f)
    assert not f.made["claude"][0].web_search
    assert "DISABLED" in f.made["claude"][0].briefing


# ---------------------------------------------------------------- admin


def test_pause_blocks_decide_and_execute(conn):
    league.set_paused(conn, True)
    with pytest.raises(LeagueError, match="paused"):
        league.decide(conn, WEEK1, factory=Factory({}))
    league.set_paused(conn, False)
    league.decide(conn, WEEK1, factory=Factory({"claude": [cash_only()], "gpt": [cash_only()]}))


def test_dry_run_writes_nothing(conn):
    f = Factory({"claude": [cash_only()], "gpt": [cash_only()]})
    with dry_run_guard(conn, True):
        league.decide(conn, WEEK1, factory=f)
    assert conn.execute("SELECT COUNT(*) FROM weeks").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 0


def test_rerunning_decide_only_runs_missing_agents(conn):
    f = Factory({"claude": [cash_only()], "gpt": [cash_only()]})
    league.decide(conn, WEEK1, factory=f)
    conn.execute("UPDATE weeks SET status='deciding'")
    conn.execute("DELETE FROM agent_turns WHERE agent_id='gpt'")
    reports = league.decide(conn, WEEK1, factory=f)
    assert [r.status for r in reports] == ["already accepted", "accepted"]


def test_deterministic_replay(conn):
    def go():
        c = _db()
        f = Factory({"claude": [[[submit([manual(1, 0.6, {"AAPL": 0.4, "XOM": -0.2, "TLT": 0.4})])]]],
                     "gpt": [[[submit([manual(1, 1.0, {"SPY": 0.25, "MSFT": -0.15})])]]]})
        league.simulate(c, START, 3, factory=f, progress=lambda _: None)
        rows = [tuple(r) for r in c.execute("SELECT week_no, agent_id, weekly_return FROM scores ORDER BY 1, 2")]
        c.close()
        return rows
    assert go() == go()
