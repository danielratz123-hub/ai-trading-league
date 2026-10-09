"""Weekly strategy reports: engine numbers, the read-only report turn,
preserve counts, the briefing, and the dashboard's privacy rule."""
import json

import pytest

from arena import league, ledger, reports
from arena.db import connect
from arena.trading_calendar import store_days

from .fakes import Factory, manual, submit
from .test_cli_agents import FakeCli
from .test_tools import _market

START = "2024-01-02"


@pytest.fixture
def played(tmp_path):
    """Three simulated weeks: Claude runs two slots (one long/short), GPT one."""
    path = tmp_path / "arena.db"
    conn = connect(path)
    df = _market()
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(conn, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    week = [submit([manual(1, 0.5, {"AAPL": 0.4, "XOM": -0.25}, name="pair"), manual(2, 0.4, {"SPY": 0.5}, name="core")])]
    f = Factory({"claude": [[week]] * 3, "gpt": [[[submit([manual(1, 1.0, {"MSFT": 0.2}, name="msft")])]]] * 3})
    league.simulate(conn, START, 3, factory=f, progress=lambda _: None)
    yield path, conn
    conn.close()


def good_report(numbers, keys=("ride-winners",)):
    return {"summary": "Fine week.", "slots": [
        {"slot": s["slot"], "what_worked": "w", "what_didnt": "d", "what_to_improve": "i",
         "what_to_preserve": [{"key": k, "text": f"keep {k}"} for k in keys]} for s in numbers["slots"]]}


def test_engine_numbers_add_up(played):
    _, conn = played
    n = reports.week_numbers(conn, "claude", 1)
    assert n and [s["slot"] for s in n["slots"]] == [1, 2]
    sc = conn.execute("SELECT * FROM scores WHERE week_no=1 AND agent_id='claude'").fetchone()
    assert n["weekly_return"] == pytest.approx(sc["weekly_return"], abs=1e-6)
    for s in n["slots"]:
        # Per-ticker P&L adds up to the slot's equity change over the week.
        assert sum(t["pnl"] for t in s["tickers"]) == pytest.approx(s["pnl_usd"], abs=0.05)
        assert s["contribution"] == pytest.approx(s["pnl_usd"] / sc["start_equity"], abs=1e-5)
        assert s["costs"]["total"] > 0          # the week's opening rebalance had costs
        assert 0 <= s["hit_rate"] <= 1
    pair = n["slots"][0]
    assert {t["ticker"] for t in pair["tickers"]} == {"AAPL", "XOM"}
    assert any(t["side"] == "short" for t in pair["tickers"])
    # Slot contributions add up to the whole account's weekly P&L.
    total = sum(s["pnl_usd"] for s in n["slots"])
    assert total == pytest.approx(sc["end_equity"] - sc["start_equity"], abs=0.05)


def test_shares_survive_a_split(played):
    _, conn = played
    from arena.reports import _shares_at
    seq_after = conn.execute("SELECT MAX(seq) FROM trades WHERE agent_id='gpt'").fetchone()[0]
    before = _shares_at(conn, "gpt", 1, "MSFT", seq_after)
    # A 2:1 split later: positions are divided by the factor (0.5) -> doubled.
    ledger.apply_price_adjustment(conn, "MSFT", 0.5, "2024-02-28")
    seq_now = conn.execute("SELECT MAX(seq) FROM corporate_actions").fetchone()[0]
    assert _shares_at(conn, "gpt", 1, "MSFT", seq_now) == pytest.approx(before * 2)
    assert _shares_at(conn, "gpt", 1, "MSFT", seq_after) == pytest.approx(before)    # history unchanged


def test_report_validation():
    numbers = {"slots": [{"slot": 1}, {"slot": 2}]}
    assert reports.check_report(good_report(numbers), numbers) == []
    missing = good_report(numbers)
    missing["slots"].pop()
    assert any("one entry per slot" in e for e in reports.check_report(missing, numbers))
    bad_key = good_report(numbers, keys=("Bad Key!",))
    assert any("key like" in e for e in reports.check_report(bad_key, numbers))
    empty = good_report(numbers)
    empty["slots"][0]["what_worked"] = " "
    assert any("what_worked is missing" in e for e in reports.check_report(empty, numbers))


def cli_factory(conn, keys_by_agent):
    """FakeCli agents that read their numbers, then submit a report with
    deliberately WRONG numbers mixed in (the engine must ignore them)."""
    def make(agent_id):
        week = max(r[0] for r in conn.execute("SELECT week_no FROM weeks WHERE status='scored'"))
        n = reports.week_numbers(conn, agent_id, week)
        rep = good_report(n, keys_by_agent[agent_id])
        rep["slots"][0]["slot_return"] = 9.99            # made-up number
        return FakeCli(agent_id, [[["get_week_numbers", {}], ["get_week_trades", {}], ["submit_report", rep]]])
    return make


def test_report_turn_end_to_end_and_preserve_counts(played):
    path, conn = played
    done = reports.run_reports(conn, cli_factory(conn, {"claude": ["ride-winners"], "gpt": ["stay-concentrated"]}), simulate=True)
    assert {(d["week"], d["agent"], d["status"]) for d in done} == {
        (2, "claude", "accepted"), (2, "gpt", "accepted"), (3, "claude", "accepted"), (3, "gpt", "accepted")}
    row = conn.execute("SELECT * FROM strategy_reports WHERE week_no=3 AND agent_id='claude'").fetchone()
    stored = json.loads(row["report_json"])
    n = reports.week_numbers(conn, "claude", 3)
    assert stored["slots"][0]["slot_return"] == n["slots"][0]["slot_return"] != 9.99   # engine's number
    assert stored["slots"][0]["what_to_preserve"][0] == {"key": "ride-winners", "text": "keep ride-winners", "times": 2}
    assert "Preserve [ride-winners] (x2)" in row["report_text"]
    assert row["tool_calls"] == 2
    assert reports.preserve_counts(conn, "claude")["ride-winners"]["times"] == 2
    # Second run: nothing left to do.
    assert reports.run_reports(conn, cli_factory(conn, {"claude": ["x"], "gpt": ["y"]})) == []

    # The next briefing shows the last reports and the repeated lesson.
    from arena.agents import briefing
    text = briefing.build(conn, "claude", 4, "2024-01-26", "2024-01-29", False, 40, 0.0)
    assert "Your strategy reports" in text and "Week 3 report (claude)" in text
    assert "ride-winners x2" in text
    assert "Soft limit: about" in text
    assert "stay-concentrated" not in text                              # never the opponent's


def test_report_tools_are_read_only_and_own_data_only(played, tmp_path):
    path, conn = played
    import argparse

    from arena.mcp_server import ReportServer
    args = argparse.Namespace(db=str(path), agent="claude", week=2, result=str(tmp_path / "r.json"))
    srv = ReportServer(args)
    try:
        trades = json.loads(srv.call("get_week_trades", {})[0])
        assert trades and all(t["ticker"] != "MSFT" for t in trades)     # MSFT is only GPT's
        with pytest.raises(Exception, match="readonly"):
            srv.conn.execute("DELETE FROM trades")
        names = {s["name"] for s in srv.list_tools()}
        assert names == {"get_week_numbers", "get_week_trades", "get_my_decision", "get_past_reports", "submit_report"}
        for _ in range(20):
            text, err = srv.call("get_week_numbers", {})
        assert err and "budget" in text
    finally:
        srv.close()


def test_dashboard_shows_reports_only_for_scored_weeks(played, monkeypatch):
    path, conn = played
    reports.run_reports(conn, cli_factory(conn, {"claude": ["a-b"], "gpt": ["c-d"]}), simulate=True)
    conn.execute("UPDATE strategy_reports SET input_tokens=1000, output_tokens=200")   # usage, as a real CLI reports it
    # A report row for a week that isn't scored must stay hidden.
    conn.execute("INSERT INTO weeks(week_no, decision_date, exec_date, status) VALUES (9, '2024-03-01', '2024-03-04', 'decided')")
    conn.execute("INSERT INTO strategy_reports(week_no, agent_id, status, report_text, started_at) "
                 "VALUES (9, 'claude', 'accepted', 'SECRET', ?)", (reports._now(),))
    from fastapi.testclient import TestClient

    import arena.api.app as app_module
    monkeypatch.setenv("ARENA_DB", str(path))
    monkeypatch.delenv("DASHBOARD_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("ARENA_SERVER", raising=False)
    c = TestClient(app_module.create_app())
    data = c.get("/api/reports").json()
    assert [w["week"] for w in data["weeks"]] == [3, 2]
    assert "SECRET" not in json.dumps(data)
    assert data["preserve"]["claude"][0]["key"] == "a-b"
    costs = c.get("/api/costs").json()
    month = costs["monthly"][0]["agents"]["claude"]
    assert month["report_turns"] == 3 and month["est_ai_cost_usd"] > 0     # 2 reports + the fake row
    st = c.get("/api/status").json()
    assert st["data_feed_label"] == "IEX feed" and st["universe"]["members"] > 400
