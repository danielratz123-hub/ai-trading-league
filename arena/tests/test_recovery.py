"""Fixes from the code review: corporate actions on execution day, missed
execution days, interrupted turns/weeks, and robustness."""
import json

import pandas as pd
import pytest

from arena import league, ledger, prices
from arena.agents.cli import CliAgent
from arena.ledger import SlotTarget

from .conftest import DAYS, flat_prices, set_price
from .fakes import Factory, ScriptedAgent, manual, submit
from .test_tools import _db

D1, D2, D3, D4 = DAYS[1], DAYS[2], DAYS[3], DAYS[4]


def add_decided_week(conn, decision_date, exec_date, targets_by_agent):
    conn.execute("INSERT INTO weeks(week_no, decision_date, exec_date, status) VALUES "
                 "((SELECT COALESCE(MAX(week_no),0)+1 FROM weeks), ?, ?, 'decided')", (decision_date, exec_date))
    for agent, targets in targets_by_agent.items():
        conn.execute("INSERT INTO pending_orders(agent_id, created_date, exec_date, kind, payload) "
                     "VALUES (?,?,?,'rebalance',?)",
                     (agent, decision_date, exec_date, json.dumps([t.__dict__ for t in targets])))


# ---------------------------------------------------------------- split on execution day


class SplitSource:
    """History as Alpaca would serve it on the morning of a 4:1 split:
    every past bar already divided by 4, and today's open on the new scale."""

    def __init__(self, closes, opens_today):
        self.closes, self.opens_today = closes, opens_today

    def fetch(self, tickers, start, end):
        rows = [(t, d, c, c, c, c, 1e6) for t in tickers for d, c in self.closes.get(t, {}).items()
                if start <= d <= end]
        return pd.DataFrame(rows, columns=prices.BAR_COLUMNS)

    def fetch_opens(self, tickers, d):
        return pd.DataFrame([(t, self.opens_today[t]) for t in tickers if t in self.opens_today],
                            columns=["ticker", "open"])


def test_split_on_execution_day_is_applied_before_trading_at_the_open(conn):
    flat_prices(conn, "NVDA", 100, DAYS[:3])
    flat_prices(conn, "SPY", 400, DAYS[:3])
    ledger.create_agent(conn, "claude", 1000, DAYS[0])
    ledger.rebalance(conn, "claude", D1, [SlotTarget(1, 0.5, {"NVDA": 0.5})])
    shares_before = ledger.position_shares(conn, "claude", 1, "NVDA")
    equity_before = ledger.value_agent(conn, "claude", D2).equity
    ledger.snapshot(conn, "claude", D1, "post_trade", "open")
    add_decided_week(conn, D2, D3, {"claude": [SlotTarget(1, 0.5, {"NVDA": 0.5})]})

    # Morning of D3: NVDA splits 4:1.
    src = SplitSource({"NVDA": {d: 25.0 for d in DAYS[:3]}, "SPY": {d: 400.0 for d in DAYS[:3]}},
                      {"NVDA": 25.0, "SPY": 400.0})
    assert league.prepare_open(conn, D3, src) == []
    assert ledger.position_shares(conn, "claude", 1, "NVDA") == pytest.approx(shares_before * 4)
    rep = league.run_open(conn, D3)
    mark = conn.execute("SELECT equity FROM snapshots WHERE kind='mark' AND date=?", (D3,)).fetchone()[0]
    assert mark == pytest.approx(equity_before, rel=1e-6)            # no fake 75% loss
    # Same target weights, so the rebalance barely trades (no 4x buy).
    nvda = conn.execute("SELECT shares FROM trades WHERE date=? AND ticker='NVDA'", (D3,)).fetchall()
    assert all(abs(r[0]) < 0.5 for r in nvda)
    assert rep.weekly is not None


# ---------------------------------------------------------------- missed execution day


def test_missed_execution_day_fills_at_the_next_open_not_the_old_one(conn):
    flat_prices(conn, "SPY", 400, DAYS[:2])
    set_price(conn, "SPY", D2, 400, 440)
    set_price(conn, "SPY", D3, 450, 450)
    ledger.create_agent(conn, "claude", 1000, DAYS[0])
    add_decided_week(conn, D1, D2, {"claude": [SlotTarget(1, 1.0, {"SPY": 0.25})]})
    # The D2 morning run never happened; we're now at D3's open.
    rep = league.run_open(conn, D3)
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert t["date"] == D3 and t["ref_price"] == 450                  # not D2's 400 (look-ahead)
    assert conn.execute("SELECT exec_date FROM weeks").fetchone()[0] == D3
    assert rep.weekly.exec_date == D3


def test_if_the_day_already_closed_the_week_moves_to_the_next_open(conn):
    flat_prices(conn, "SPY", 400, DAYS[:5])
    ledger.create_agent(conn, "claude", 1000, DAYS[0])
    add_decided_week(conn, D1, D2, {"claude": [SlotTarget(1, 1.0, {"SPY": 0.25})]})
    league.daily(conn, D2)                       # execute didn't run; daily did
    rep = league.run_open(conn, D2)              # someone runs execute for D2 late
    assert rep.weekly is None and conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert conn.execute("SELECT exec_date FROM weeks").fetchone()[0] == D3
    assert league.run_open(conn, D3).weekly.exec_date == D3


# ---------------------------------------------------------------- interrupted turns and weeks


WEEK1, WEEK2 = "2024-01-05", "2024-01-12"


@pytest.fixture
def market():
    c = _db()
    yield c
    c.close()


def test_turn_left_running_by_a_crash_is_rerun(market):
    conn = market
    f = Factory({"claude": [[[submit([])]]], "gpt": [[[submit([])]]]})
    league.decide(conn, WEEK1, factory=f)
    # Simulate a crash mid-turn: claude's row stuck at 'running', week still deciding.
    conn.execute("UPDATE agent_turns SET status='running' WHERE agent_id='claude'")
    conn.execute("UPDATE weeks SET status='deciding'")
    reports = {r.agent_id: r.status for r in league.decide(conn, WEEK1, factory=f)}
    assert reports == {"claude": "accepted", "gpt": "already accepted"}
    assert conn.execute("SELECT COUNT(*) FROM agent_turns WHERE agent_id='claude'").fetchone()[0] == 1


def test_interrupted_week_is_skipped_instead_of_blocking_the_league(market):
    conn = market
    f = Factory({"claude": [[[submit([manual(1, 1.0, {"SPY": 0.25})])]]], "gpt": [[[submit([])]]]})
    league.decide(conn, WEEK1, factory=f)
    conn.execute("UPDATE weeks SET status='deciding'")                 # crashed, never finished
    conn.execute("UPDATE agent_turns SET status='running' WHERE agent_id='gpt'")
    league.decide(conn, WEEK2, factory=f)                              # next weekend: no deadlock
    weeks = {r["week_no"]: r["status"] for r in conn.execute("SELECT * FROM weeks")}
    assert weeks == {1: "skipped", 2: "decided"}
    assert conn.execute("SELECT status FROM agent_turns WHERE week_no=1 AND agent_id='gpt'").fetchone()[0] == "failed"
    assert conn.execute("SELECT status FROM pending_orders WHERE created_date=?", (WEEK1,)).fetchone()[0] == "skipped"
    rep = league.execute(conn)
    assert rep.week_no == 2


def test_agent_without_an_adapter_is_reported_not_a_crash(market):
    conn = market
    ledger.create_agent(conn, "llama", 1000, WEEK1)
    scripts = {"claude": [[submit([])]], "gpt": [[submit([])]]}

    def factory(agent_id):
        if agent_id not in scripts:
            raise ValueError(f"no adapter for {agent_id}")
        return ScriptedAgent(agent_id, scripts[agent_id])
    reports = {r.agent_id: r for r in league.decide(conn, WEEK1, factory=factory)}
    assert reports["llama"].status == "failed" and "no adapter" in reports["llama"].error
    assert reports["claude"].status == reports["gpt"].status == "accepted"


class BrokenCli(CliAgent):
    def command(self, workdir, mcp, web_search, resume_session):
        return ["Z:/definitely/not/installed/claude.exe"]

    def parse_event(self, ev, run):
        pass


def test_cli_that_cannot_start_returns_an_error(tmp_path):
    run = BrokenCli("claude", "").run("prompt", tmp_path, None, web_search=False)
    assert run.error and "Could not start" in run.error


# ---------------------------------------------------------------- price update window


class RecordingSource:
    def __init__(self):
        self.calls = []

    def fetch(self, tickers, start, end):
        self.calls.append((tuple(tickers), start))
        return pd.DataFrame(columns=prices.BAR_COLUMNS)


def test_one_stale_ticker_does_not_widen_everyone_elses_download(conn):
    flat_prices(conn, "AAPL", 100, DAYS[:40])
    flat_prices(conn, "MSFT", 100, DAYS[:40])
    flat_prices(conn, "OLD", 100, DAYS[:3])          # stopped trading long ago
    src = RecordingSource()
    prices.update_prices(conn, src, ["AAPL", "MSFT", "OLD"], end=DAYS[40], history_start=DAYS[0])
    starts = {tickers: start for tickers, start in src.calls}
    assert starts[("AAPL", "MSFT")] > DAYS[30]       # a few days before their own last bar
    assert ("OLD",) in starts                        # the stale one is fetched on its own


def test_week_decided_after_the_open_waits_for_the_next_open(conn):
    flat_prices(conn, "SPY", 400, DAYS[:5])
    ledger.create_agent(conn, "claude", 1000, DAYS[0])
    add_decided_week(conn, D1, D2, {"claude": [SlotTarget(1, 1.0, {"SPY": 0.25})]})
    # The agent finished its turn at 10:14 ET on D3, after that day's 9:30 open.
    conn.execute("INSERT INTO agent_turns(week_no, agent_id, model, status, started_at, finished_at) "
                 "VALUES (1, 'claude', 'm', 'accepted', ?, ?)", (f"{D3}T15:00:00+00:00", f"{D3}T15:14:00+00:00"))   # 10:14 ET (EST = UTC-5)
    rep = league.run_open(conn, D3)
    assert rep.weekly is None and conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    assert conn.execute("SELECT exec_date FROM weeks").fetchone()[0] == D4
    assert league.run_open(conn, D4).weekly.exec_date == D4


def test_real_alpaca_source_has_opening_prices(monkeypatch):
    """Guards against fetch_opens drifting out of the class (a real bug once)."""
    from types import SimpleNamespace as NS
    src = prices.AlpacaPriceSource.__new__(prices.AlpacaPriceSource)
    src.feed = "sip"
    idx = pd.MultiIndex.from_tuples(
        [("AAPL", pd.Timestamp("2024-01-03 14:31", tz="UTC")), ("AAPL", pd.Timestamp("2024-01-03 14:30", tz="UTC")),
         ("SPY", pd.Timestamp("2024-01-03 14:30", tz="UTC"))], names=["symbol", "timestamp"])
    bars = pd.DataFrame({"open": [190.5, 190.0, 470.0], "close": [191, 190.4, 471]}, index=idx)
    src.client = NS(get_stock_bars=lambda req: NS(df=bars))
    opens = src.fetch_opens(["AAPL", "SPY"], "2024-01-03").set_index("ticker")["open"].to_dict()
    assert opens == {"AAPL": 190.0, "SPY": 470.0}            # the FIRST minute of the day


# ---------------------------------------------------------------- starting mid-day


class LatestSource:
    def __init__(self, quotes):            # {ticker: (price, "2024-01-05T16:00:00+00:00")}
        self.quotes, self.feed = quotes, "test"

    def fetch(self, tickers, start, end):
        return pd.DataFrame(columns=prices.BAR_COLUMNS)

    def fetch_latest(self, tickers, after):
        return pd.DataFrame([(t, p, pd.Timestamp(ts)) for t, (p, ts) in self.quotes.items() if t in tickers],
                            columns=["ticker", "price", "time"])


def test_replan_then_trade_now_at_a_price_after_the_decisions(market):
    conn = market
    f = Factory({"claude": [[[submit([manual(1, 1.0, {"SPY": 0.25})])]]], "gpt": [[[submit([])]]]})
    league.decide(conn, WEEK1, factory=f)
    assert league.reset_unexecuted_start(conn) == 1                      # untraded: can be redone
    assert conn.execute("SELECT COUNT(*) FROM agent_turns").fetchone()[0] == 0
    league.decide(conn, "2024-01-09", factory=f)                          # mid-week, fresher data
    week = conn.execute("SELECT * FROM weeks").fetchone()
    assert (week["week_no"], week["exec_date"]) == (1, "2024-01-10")       # still week 1
    conn.execute("UPDATE agent_turns SET finished_at='2024-01-10T15:10:00+00:00'")   # decided 10:10 ET

    too_old = LatestSource({"SPY": (100.0, "2024-01-10T15:05:00+00:00")})
    with pytest.raises(league.LeagueError, match="older than the decisions"):
        league.execute_now(conn, "2024-01-10", too_old)
    rep = league.execute_now(conn, "2024-01-10", LatestSource({"SPY": (123.45, "2024-01-10T16:30:00+00:00")}))
    t = conn.execute("SELECT * FROM trades").fetchone()
    assert rep.trades["claude"] == 1 and t["ref_price"] == 123.45 and t["date"] == "2024-01-10"
    # The fill price is what valuations of that open use from now on.
    assert prices.get_price(conn, "SPY", "2024-01-10", "open") == 123.45
    with pytest.raises(league.LeagueError, match="already"):
        league.reset_unexecuted_start(conn)                               # money moved: no redo


def test_buys_never_leave_a_slot_with_negative_cash(conn):
    flat_prices(conn, "AAPL", 100)
    flat_prices(conn, "MSFT", 100)
    ledger.create_agent(conn, "gpt", 1000, DAYS[0])
    ledger.rebalance(conn, "gpt", D1, [SlotTarget(1, 1.0, {"AAPL": 0.5, "MSFT": 0.5})])   # 100% invested
    assert ledger.slot_cash(conn, "gpt")[1] >= -1e-9


def test_complete_week_buys_skipped_positions_and_fixes_negative_cash(market):
    conn = market
    f = Factory({"claude": [[[submit([manual(1, 0.5, {"SPY": 0.5, "XOM": 0.5})])]]],
                 "gpt": [[[submit([manual(1, 1.0, {"MSFT": 0.25})])]]]})
    league.decide(conn, "2024-01-09", factory=f)
    conn.execute("UPDATE agent_turns SET finished_at='2024-01-10T15:10:00+00:00'")
    conn.execute("DELETE FROM prices WHERE date >= '2024-01-10'")     # mid-day: no daily bars yet
    league.execute_now(conn, "2024-01-10", LatestSource({"SPY": (470.0, "2024-01-10T16:00:00+00:00"),
                                                       "MSFT": (380.0, "2024-01-10T16:00:00+00:00")}))
    assert {p["ticker"] for p in ledger.positions(conn, "claude")} == {"SPY"}     # XOM had no price
    conn.execute("UPDATE slots SET cash = -1.9 WHERE agent_id='gpt' AND slot=1")  # an old overdraw
    done = league.complete_week_now(conn, "2024-01-10", LatestSource({
        "XOM": (100.0, "2024-01-10T16:20:00+00:00"), "SPY": (471.0, "2024-01-10T16:20:00+00:00"),
        "MSFT": (381.0, "2024-01-10T16:20:00+00:00")}))
    assert any("bought" in x and "XOM" in x for x in done)
    xom = ledger.position_shares(conn, "claude", 1, "XOM") * 100.0
    assert xom == pytest.approx(0.5 * 0.5 * 1000, rel=0.02)                      # the planned weight
    assert ledger.slot_cash(conn, "gpt")[1] >= 0
    assert league.complete_week_now(conn, "2024-01-10", LatestSource({})) == []    # nothing left to do


def test_trades_carry_the_agents_reasons(market):
    conn = market
    slot = manual(1, 1.0, {"SPY": 0.25, "XOM": 0.2}, name="Core")
    slot["position_reasons"] = {"SPY": "broad market exposure"}
    slot["reason"] = "steady core"
    f = Factory({"claude": [[[submit([slot])]]], "gpt": [[[submit([])]]]})
    league.decide(conn, WEEK1, factory=f)
    league.execute(conn)
    notes = {r["ticker"]: r["note"] for r in conn.execute("SELECT ticker, note FROM trades")}
    assert notes["SPY"] == "broad market exposure"            # the agent's own words
    assert notes["XOM"] == "Core: steady core"                # falls back to the slot's reason


def test_old_trades_get_reasons_backfilled(market):
    conn = market
    slot = manual(1, 1.0, {"SPY": 0.25}, name="Core")
    slot["reason"] = "steady core"
    f = Factory({"claude": [[[submit([slot])]]], "gpt": [[[submit([])]]]})
    league.decide(conn, WEEK1, factory=f)
    league.execute(conn)
    conn.execute("UPDATE trades SET note = NULL")              # as if made by the older version
    assert league.backfill_trade_notes(conn) == 1
    assert conn.execute("SELECT note FROM trades").fetchone()[0] == "Core: steady core"
