"""Grader math, frozen decisions, and the anti-lookahead rule."""
from __future__ import annotations

import sqlite3

import pytest

from arena import ledger
from arena.learning import grader, journal, lessons, review, strategies as strat

from .learning_helpers import DECIDED_AT, league_week1, prices, set_price

# Trading days after EXEC (2024-01-08): 01-09, 01-10, 01-11, 01-12, 01-16 (01-15 is a holiday), 01-17


@pytest.fixture
def league(conn, monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    prices(conn)
    s = league_week1(conn, through="2024-01-17")
    return conn, s


def decide(conn, s, action, symbol, decided_at=DECIDED_AT, slot=1):
    sha = journal.store_input(conn, "the exact prompt")
    return journal.record(conn, agent_id="claude", version_id=s[slot].version_id, checkpoint="weekly_rebalance",
                          input_sha=sha, input_kind="test", reasoning="because", action=action,
                          confidence=0.6, symbol=symbol, week_no=1, decided_at=decided_at)


def test_symbol_grades(league):
    conn, s = league
    # No linked fill: entry = the next open after the decision (Monday 01-08, open 100).
    set_price(conn, "XOM", "2024-01-09", 100, 102)      # day 1 close +2%
    set_price(conn, "XOM", "2024-01-10", 100, 95)       # low point (low = 95)
    set_price(conn, "XOM", "2024-01-16", 100, 104)      # day 5 close +4%
    set_price(conn, "SPY", "2024-01-16", 100, 101)      # SPY +1% over the week
    did = decide(conn, s, "buy", "XOM")
    stats = grader.grade_all(conn)
    assert stats["updated"] >= 1
    o = conn.execute("SELECT * FROM outcomes WHERE decision_id=?", (did,)).fetchone()
    assert o["pnl_1d_pct"] == pytest.approx(2.0)
    assert o["pnl_1w_pct"] == pytest.approx(4.0)
    assert o["vs_spy_1w_pct"] == pytest.approx(3.0)
    assert o["max_drawdown_pct"] == pytest.approx(5.0)
    assert o["pnl_exit_pct"] is None


def test_short_direction_and_exit(league):
    conn, s = league
    set_price(conn, "MSFT", "2024-01-09", 100, 90)      # short wins 10% on day 1
    did = decide(conn, s, "short", "MSFT", slot=1)
    conn.execute("UPDATE trades SET decision_id=? WHERE ticker='MSFT' AND slot=1", (did,))
    # The position is closed on 01-10 by a stop at 97 -> +3% for the... entry was a buy fill though:
    tr = conn.execute("SELECT * FROM trades WHERE decision_id=?", (did,)).fetchone()
    ledger.execute_trade(conn, "claude", 1, "MSFT", -tr["shares"], "2024-01-10", 97.0, "stop_loss")
    grader.grade_all(conn)
    o = conn.execute("SELECT * FROM outcomes WHERE decision_id=?", (did,)).fetchone()
    ref = tr["fill_price"]
    assert o["pnl_1d_pct"] == pytest.approx(-(90 / ref - 1) * 100, abs=1e-3)
    assert o["exit_reason"] == "stop_loss"
    exit_fill = conn.execute("SELECT fill_price FROM trades WHERE reason='stop_loss'").fetchone()[0]
    assert o["pnl_exit_pct"] == pytest.approx(-(exit_fill / ref - 1) * 100, abs=1e-3)


def test_hold_is_graded_on_the_strategy(league):
    conn, s = league
    did = decide(conn, s, "hold", None, slot=2)
    grader.grade_all(conn)
    o = conn.execute("SELECT * FROM outcomes WHERE decision_id=?", (did,)).fetchone()
    assert o is not None and o["pnl_1w_pct"] == pytest.approx(0.0, abs=1e-6)   # flat prices


def test_decisions_are_frozen(league):
    conn, s = league
    did = decide(conn, s, "buy", "XOM")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE decisions SET reasoning='rewritten' WHERE id=?", (did,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM decisions WHERE id=?", (did,))
    grader.grade_all(conn)
    assert conn.execute("SELECT COUNT(*) FROM outcomes_history").fetchone()[0] >= 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE outcomes_history SET pnl_1d_pct=99")
    assert journal.read_input(conn, conn.execute("SELECT json_extract(input_snapshot, '$.sha256') FROM decisions "
                                                 "WHERE id=?", (did,)).fetchone()[0]) == "the exact prompt"


def test_regrade_appends_history(league):
    conn, s = league
    did = decide(conn, s, "buy", "XOM")
    set_price(conn, "XOM", "2024-01-09", 100, 102)
    grader.grade_all(conn, now="2024-01-09T22:00:00+00:00")
    set_price(conn, "XOM", "2024-01-16", 100, 110)
    grader.grade_all(conn, now="2024-01-16T22:00:00+00:00")
    hist = conn.execute("SELECT evaluated_at, pnl_1w_pct FROM outcomes_history WHERE decision_id=? ORDER BY id",
                        (did,)).fetchall()
    assert len(hist) == 2 and hist[-1]["pnl_1w_pct"] == pytest.approx(10.0)


# --------------------------------------------------------------------------
# Anti-lookahead
# --------------------------------------------------------------------------

def test_review_as_of_a_past_time_never_sees_later_outcomes(league):
    conn, s = league
    early = decide(conn, s, "buy", "XOM")
    late = decide(conn, s, "buy", "KO", decided_at="2024-01-11T15:00:00+00:00")
    set_price(conn, "XOM", "2024-01-09", 100, 102)
    grader.grade_all(conn, now="2024-01-09T22:00:00+00:00")    # XOM: 1-day grade only
    set_price(conn, "XOM", "2024-01-16", 100, 120)
    set_price(conn, "KO", "2024-01-16", 100, 80)
    grader.grade_all(conn, now="2024-01-17T22:00:00+00:00")    # later: XOM's week, KO's grades
    lessons.upsert(conn, agent_id="claude", key="later-lesson", lesson="learned later", kind="keep",
                   evidence=[early], day="2024-01-17", at="2024-01-17T22:30:00+00:00")
    conn.execute("UPDATE lessons SET status='active'")

    as_of = "2024-01-10T21:00:00+00:00"
    seen = grader.outcomes_as_of(conn, "claude", as_of)
    assert [o["decision_id"] for o in seen] == [early]          # KO was decided later
    assert seen[0]["pnl_1d_pct"] == pytest.approx(2.0)
    assert seen[0]["pnl_1w_pct"] is None                         # the week's grade came later

    inp, lesson_ids = review.build_input(conn, "claude", "daily", as_of, "2024-01-10")
    text = review.render_prompt("daily", inp)
    assert lesson_ids == [] and "later-lesson" not in text
    graded = inp["graded_decisions_since_last_review"]
    assert {g["decision_id"] for g in graded} <= {early}
    assert all(g["pnl_1w_pct"] is None for g in graded)
    assert "120" not in text and "80.0" not in text

    # The same review run today sees everything.
    now_seen = grader.outcomes_as_of(conn, "claude", "2024-01-18T00:00:00+00:00")
    assert {o["decision_id"] for o in now_seen} == {early, late}


def test_market_context_as_of(league):
    conn, s = league
    for day, ts, regime in [("2024-01-09", "2024-01-09T21:00:00+00:00", "choppy"),
                            ("2024-01-12", "2024-01-12T21:00:00+00:00", "high_vol")]:
        conn.execute("INSERT INTO market_context(ts, day, kind, regime, sector_moves, headlines, regime_inputs) "
                     "VALUES (?,?, 'close', ?, '{}', '[]', '{}')", (ts, day, regime))
    inp, _ = review.build_input(conn, "claude", "daily", "2024-01-10T21:00:00+00:00", "2024-01-10")
    assert inp["regime"]["current"] == "choppy"
    assert [c["day"] for c in inp["market_context_last_5_days"]] == ["2024-01-09"]


def test_strategy_summary_numbers(league):
    conn, s = league
    inp, _ = review.build_input(conn, "claude", "daily", "2024-01-12T22:00:00+00:00", "2024-01-12")
    s1 = next(x for x in inp["strategies"] if x["slot"] == 1)
    assert s1["strategy"] == "momentum" and s1["params"]["stop_loss_pct"] == 0.06
    assert s1["equity"] == pytest.approx(500, rel=0.01)
    assert len(s1["open_positions"]) == 5 and all(p["stop_loss"] for p in s1["open_positions"])
    assert s1["in_cooldown"] is False and s1["trades_since_start"] == 5
    assert strat.in_slot(conn, "claude", 2).name == "defensive"
