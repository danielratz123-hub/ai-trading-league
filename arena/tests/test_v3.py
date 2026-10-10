"""Read-only data behind the V3 dashboard."""
import pytest

from arena import league, ledger
from arena.api import v3
from arena.db import connect
from arena.ledger import SlotTarget
from arena.trading_calendar import store_days

from .fakes import Factory, manual, submit
from .test_tools import _market


def with_exits(sub, exits):
    name, args = sub
    return name, {**args, "exit_reasons": exits}


@pytest.fixture
def played(tmp_path):
    conn = connect(tmp_path / "a.db")
    df = _market()
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(conn, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    w1 = [submit([manual(1, 0.5, {"AAPL": 0.4}, name="Momentum"), manual(2, 0.3, {"XOM": -0.3}, name="Energy short")],
                 view="Week one view", notes="remember X")]
    w2 = [with_exits(submit([manual(1, 0.5, {"MSFT": 0.4}, name="Momentum"), manual(3, 0.2, {"SPY": 0.5}, name="Core")],
                            view="Week two view"), {"AAPL": "Momentum faded", "XOM": "Oil turned up"})]
    w3 = [submit([manual(1, 0.6, {"MSFT": 0.4}, name="Momentum"), manual(3, 0.2, {"SPY": 0.5}, name="Core")],
                 view="Week three view")]
    f = Factory({"claude": [[w1], [w2], [w3]], "gpt": [[[submit([manual(1, 1.0, {"SPY": 0.2})])]]] * 3})
    league.simulate(conn, "2024-01-02", 3, factory=f, progress=lambda _: None)
    yield conn
    conn.close()


def test_closed_trades_entry_exit_hold_and_why(played):
    conn = played
    done = {(c["agent"], c["ticker"]): c for c in v3.closed_trades(conn)}
    aapl, xom = done[("claude", "AAPL")], done[("claude", "XOM")]
    assert aapl["side"] == "LONG" and xom["side"] == "SHORT"
    assert aapl["why"] == "Momentum faded" and xom["why"] == "Oil turned up"       # exit_reasons
    assert aapl["held_days"] == 5 and aapl["entry"] > 0 and aapl["exit"] > 0
    # Net P&L = realized on the closing trades - all commissions of the position.
    rows = conn.execute("SELECT realized_pnl, commission FROM trades WHERE agent_id='claude' AND ticker='AAPL'").fetchall()
    assert aapl["net_pnl"] == pytest.approx(sum(r[0] for r in rows) - sum(r[1] for r in rows), abs=0.01)
    assert ("claude", "MSFT") not in done                                         # still open


def test_flip_and_partial_closes_and_a_split(tmp_path):
    from .conftest import DAYS, flat_prices, set_price
    conn = connect(tmp_path / "b.db")
    store_days(conn, [(d, "09:30", "16:00") for d in DAYS])
    for t in ("SPY", "AAPL"):
        flat_prices(conn, t, 100.0)
    ledger.create_agent(conn, "claude", 1000, DAYS[0])
    ledger.rebalance(conn, "claude", DAYS[0], [SlotTarget(1, 1.0, {"AAPL": 0.2})])     # open long ~2 sh
    ledger.rebalance(conn, "claude", DAYS[1], [SlotTarget(1, 1.0, {"AAPL": 0.1})])     # partial close
    assert v3.closed_trades(conn) == []
    ledger.apply_price_adjustment(conn, "AAPL", 0.5, DAYS[2])                          # 2:1 split
    for d in DAYS[2:]:
        set_price(conn, "AAPL", d, 50.0)
    ledger.rebalance(conn, "claude", DAYS[3], [SlotTarget(1, 1.0, {"AAPL": -0.1})])    # flip to short
    done = v3.closed_trades(conn)
    assert len(done) == 1 and done[0]["side"] == "LONG" and done[0]["held_days"] == 3
    ledger.rebalance(conn, "claude", DAYS[5], [SlotTarget(1, 1.0, {})])                 # close the short
    done = v3.closed_trades(conn)
    assert [d["side"] for d in done] == ["SHORT", "LONG"]                             # newest first


def test_strategies_tags_and_thesis(played):
    s = v3.strategies(played, "claude")
    by = {x["slot"]: x for x in s["strategies"]}
    assert set(by) == {1, 3}
    assert by[1]["change"] == "RETUNED" and by[3]["change"] == "KEPT"            # week 3 vs week 2
    assert by[1]["thesis"] == "test" and by[1]["name"] == "Momentum"
    assert s["cash_pct"] is not None and s["long_pct"] > 0


def test_reasoning_shows_executed_not_pending_decisions(played):
    conn = played
    r = v3.reasoning(conn, "claude")
    assert r["items"][0]["text"] == "Week three view"
    # A decided-but-not-executed week stays hidden.
    conn.execute("UPDATE weeks SET status='decided' WHERE week_no=3")
    assert v3.reasoning(conn, "claude")["items"][0]["text"] == "Week two view"


def test_overview_ticker_and_leaderboard(played):
    o = v3.overview(played)
    tick = {t["ticker"]: t for t in o["ticker"]}
    assert {"MSFT", "SPY"} <= set(tick) and tick["SPY"]["agents"] == ["claude", "gpt"]
    lb = o["leaderboard"]["agents"]["claude"]
    assert lb["weeks_scored"] == 3 and lb["best_strategy"]["name"] in ("Momentum", "Core", "Energy short")
    assert o["limits"]["max_short_per_ticker"] == 0.15


def test_exit_reasons_never_reject_a_decision():
    from arena.agents.decision import exit_reasons
    assert exit_reasons({"exit_reasons": "nonsense"}) == {}
    assert exit_reasons({"exit_reasons": {"aapl": " sold "}}) == {"AAPL": "sold"}


def test_v3_endpoints_need_login_and_never_leak_secrets(played, tmp_path, monkeypatch):
    import json as _json

    from fastapi.testclient import TestClient

    import arena.api.app as app_module
    from arena.api import auth
    db = played.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("ARENA_DB", db)
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", auth.hash_password("correct horse battery"))
    monkeypatch.setenv("ADMIN_TOKEN", "t" * 40)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "super-secret-alpaca")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    paths = ["/api/overview", "/api/strategies/claude", "/api/reasoning/gpt", "/api/closed-trades", "/", "/v3.js", "/v3.css"]
    with TestClient(app_module.create_app(), base_url="https://testserver") as c:
        for p in paths[:4]:
            assert c.get(p).status_code == 401
        c.post("/login", data={"password": "correct horse battery", "next": "/"})
        for p in paths:
            r = c.get(p)
            assert r.status_code == 200, p
            assert "super-secret-alpaca" not in r.text and "t" * 40 not in r.text
        assert c.get("/api/strategies/nobody").status_code == 404
        assert _json.loads(c.get("/api/closed-trades?limit=1").text)[0]["ticker"]
