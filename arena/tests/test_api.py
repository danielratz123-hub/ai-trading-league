"""Phase 4: the read-only dashboard API."""
import pytest
from fastapi.testclient import TestClient

from arena import league
from arena.db import connect
from arena.trading_calendar import store_days

from .fakes import Factory, manual, submit
from .test_tools import _market

CODE = "def target_weights(prices, date):\n    return {'SPY': 0.5}\n"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    path = tmp_path_factory.mktemp("api") / "arena.db"
    conn = connect(path)
    df = _market()
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(conn, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    f = Factory({
        "claude": [[[("save_strategy_code", {"slot": 2, "code": CODE})],
                    [submit([manual(1, 0.4, {"AAPL": 0.5, "XOM": -0.3}, name="<b>pair</b>"),
                             {"slot": 2, "name": "algo", "type": "code", "allocation": 0.4, "reason": "r"}],
                            view="<script>alert(1)</script> view")]]],
        "gpt": [[[submit([manual(1, 1.0, {"SPY": 0.25})], view="gpt view")]]],
    })
    league.simulate(conn, "2024-01-02", 5, factory=f, progress=lambda _: None)
    # A decided-but-unscored week whose details must stay hidden.
    league.decide(conn, league.simulation_dates(conn, "2024-01-02", 7)[6], simulate=True, factory=f)
    conn.close()

    import arena.api.app as app_module
    mp = pytest.MonkeyPatch()
    mp.setenv("ARENA_DB", str(path))
    mp.delenv("ADMIN_TOKEN", raising=False)
    yield TestClient(app_module.create_app()), path, mp
    mp.undo()


def test_status_scoreboard_and_noindex(client):
    c, _, _ = client
    r = c.get("/api/status")
    assert r.status_code == 200 and r.headers["X-Robots-Tag"] == "noindex, nofollow"
    s = r.json()
    assert {a["id"] for a in s["agents"]} == {"claude", "gpt"} and s["latest_week"]["week_no"] == 6
    sb = c.get("/api/scoreboard").json()
    assert sb["as_of"] == s["as_of"] and sb["spy_return"] is not None
    pts = {a["agent"]: a["points"] for a in sb["agents"]}
    assert sum(pts.values()) <= 5     # 5 scored weeks, ties give nobody a point
    benches = sb["benchmarks"]
    assert [b["player_id"] for b in benches] == ["bench_spy", "bench_random"]
    assert [b["label"] for b in benches] == ["SPY buy-and-hold", "Random picker"]
    for b in benches:
        assert b["total_score"] is not None and b["cumulative_return"] is not None
        assert b["last_week_return"] is not None and b["last_week_score"] is not None
    for a in sb["agents"]:
        assert "total_score" in a and "last_week_score" in a


def test_scoreboard_benchmarks_before_and_after_scoring(tmp_path, monkeypatch):
    import arena.api.app as app_module
    from arena import ledger

    db = tmp_path / "bench_api.db"
    conn = connect(db)
    df = _market()
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(conn, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    ledger.create_agent(conn, "claude", 1000, "2024-01-02")
    ledger.create_agent(conn, "gpt", 1000, "2024-01-02")
    conn.close()

    monkeypatch.setenv("ARENA_DB", str(db))
    monkeypatch.setenv("LEARNING_ENABLED", "1")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    tc = TestClient(app_module.create_app())

    # Before any scoring: benchmarks list is empty and agents have None scores.
    sb0 = tc.get("/api/scoreboard").json()
    assert sb0["benchmarks"] == []
    assert all(a["total_score"] is None and a["last_week_score"] is None for a in sb0["agents"])

    # Simulate 2 scored weeks (LEARNING_ENABLED=1 writes weekly_scores too).
    conn = connect(db)
    f = Factory({
        "claude": [[[submit([manual(1, 0.5, {"AAPL": 0.5})])]]],
        "gpt": [[[submit([manual(1, 0.5, {"SPY": 0.5})])]]],
    })
    league.simulate(conn, "2024-01-02", 2, factory=f, progress=lambda _: None)
    conn.close()

    sb1 = tc.get("/api/scoreboard").json()
    assert [b["player_id"] for b in sb1["benchmarks"]] == ["bench_spy", "bench_random"]
    assert [b["label"] for b in sb1["benchmarks"]] == ["SPY buy-and-hold", "Random picker"]
    for b in sb1["benchmarks"]:
        assert isinstance(b["total_score"], float)
        assert isinstance(b["cumulative_return"], float)
        assert isinstance(b["last_week_return"], float)
        assert isinstance(b["last_week_score"], float)
    for a in sb1["agents"]:
        assert isinstance(a["total_score"], float)
        assert isinstance(a["last_week_score"], float)


def test_equity_curves_share_dates_and_index_ignores_deposits(client):
    c, path, _ = client
    eq = c.get("/api/equity").json()
    n = len(eq["dates"])
    assert n > 20 and len(eq["spy_index"]) == n
    for a in eq["agents"].values():
        assert len(a["equity"]) == len(a["index"]) == n
    conn = connect(path)
    gpt_dep = conn.execute("SELECT COALESCE(SUM(amount),0) FROM deposits WHERE agent_id='gpt' "
                           "AND note LIKE 'bonus%'").fetchone()[0]
    conn.close()
    if gpt_dep:   # a bonus jump shows in equity but not in the index
        e, i = eq["agents"]["gpt"]["equity"], eq["agents"]["gpt"]["index"]
        jumps_e = max(abs(e[k] / e[k - 1] - 1) for k in range(1, n) if e[k] and e[k - 1])
        jumps_i = max(abs(i[k] / i[k - 1] - 1) for k in range(1, n) if i[k] and i[k - 1])
        assert jumps_e > 0.5 and jumps_i < 0.2


def test_live_stats_endpoint(client):
    c, path, _ = client
    conn = connect(path)
    before = list(conn.iterdump())
    response = c.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["as_of"] == conn.execute("SELECT MAX(date) FROM snapshots WHERE kind='daily'").fetchone()[0]
    assert {a["agent"] for a in data["agents"]} == {"claude", "gpt"}
    for agent in data["agents"]:
        assert set(agent["windows"]) == {"weekly", "monthly", "all_time"}
        assert agent["windows"]["all_time"]["equity_curve"]
        assert agent["slots"] and all(s["slot"] > 0 for s in agent["slots"])
        assert all("windows" in s for s in agent["slots"])
    assert list(conn.iterdump()) == before
    assert c.post("/api/stats").status_code == 405
    assert c.delete("/api/stats").status_code == 405
    conn.close()


def test_agent_detail_holdings_exposures_and_code(client):
    c, _, _ = client
    d = c.get("/api/agents/claude").json()
    sides = {h["ticker"]: h["side"] for h in d["holdings"]}
    assert sides.get("XOM") == "short" and sides.get("AAPL") == "long"
    labels = [g["label"] for g in d["exposures"]]
    assert labels == ["Gross exposure", "Total short", "Largest long", "Largest short"]
    assert any(s["code"] == CODE for s in d["slots"])
    assert c.get("/api/agents/nobody").status_code == 404
    slots = c.get("/api/agents/claude/slots").json()
    assert {s["slot"] for s in slots["slots"]} >= {0, 1, 2}


def test_unscored_week_is_hidden_scored_weeks_are_revealed(client):
    c, _, _ = client
    weeks = c.get("/api/weeks").json()
    latest, scored = weeks[0], weeks[1]
    assert latest["status"] == "decided"
    for a in latest["agents"].values():
        assert a["hidden"] and "market_view" not in a and "slots" not in a
    assert scored["status"] == "scored"
    claude = scored["agents"]["claude"]
    assert not claude["hidden"] and claude["market_view"].startswith("<script>")   # raw; the UI escapes it
    assert any(s.get("code") == CODE for s in claude["slots"])


def test_trades_research_costs(client):
    c, _, _ = client
    trades = c.get("/api/trades", params={"agent": "gpt", "limit": 5}).json()
    assert 0 < len(trades) <= 5 and all(t["agent"] == "gpt" for t in trades)
    research = c.get("/api/research").json()
    assert research[-1]["agents"]["claude"]["by_tool"] == {"save_strategy_code": 1}
    costs = c.get("/api/costs").json()
    assert costs["totals"]["claude"]["commissions"] > 0 and len(costs["weekly"]) == 6


def test_api_is_read_only(client):
    c, _, _ = client
    assert c.post("/api/scoreboard").status_code == 405
    assert c.delete("/api/weeks").status_code == 405


def test_admin_pause_needs_the_token(client):
    c, path, mp = client
    assert c.post("/api/admin/pause").status_code == 503          # no ADMIN_TOKEN configured
    mp.setenv("ADMIN_TOKEN", "s3cret")
    assert c.post("/api/admin/pause", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.post("/api/admin/pause", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200 and r.json() == {"paused": True, "paused_agents": []}
    assert c.get("/api/status").json()["paused"] is True
    conn = connect(path)
    with pytest.raises(league.LeagueError, match="paused"):
        league.execute(conn)
    conn.close()
    assert c.post("/api/admin/resume", headers={"Authorization": "Bearer s3cret"}).json() ==         {"paused": False, "paused_agents": []}
    # Per agent (the kill switch for one agent); unknown agents are refused.
    assert c.post("/api/admin/pause?agent=nobody", headers={"Authorization": "Bearer s3cret"}).status_code == 404
    mp.delenv("ADMIN_TOKEN")


def test_dashboard_page_is_served(client):
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "AI Trading League" in r.text
    assert c.get("/app.js").status_code == 200


def test_healthz_and_health_command(client, tmp_path):
    import subprocess
    import sys
    import time

    from arena import health
    c, path, _ = client
    assert c.get("/healthz").json() == {"ok": True}
    details = c.get("/api/health").json()
    assert details["db"] is True and "disk_free_gb" in details
    # The command Docker runs: healthy without --scheduler, unhealthy with a
    # missing heartbeat, healthy again once the heartbeat is fresh.
    cmd = [sys.executable, "-m", "arena.main", "health", "--db", str(path)]
    assert subprocess.run(cmd, capture_output=True).returncode == 0
    hb = health.heartbeat_path(path)
    hb.unlink(missing_ok=True)
    assert subprocess.run(cmd + ["--scheduler"], capture_output=True).returncode == 1
    hb.write_text(str(time.time()), encoding="utf-8")
    assert subprocess.run(cmd + ["--scheduler"], capture_output=True).returncode == 0
    hb.write_text(str(time.time() - 600), encoding="utf-8")
    r = subprocess.run(cmd + ["--scheduler"], capture_output=True, text=True)
    assert r.returncode == 1 and "heartbeat" in r.stdout
    # A missing database is unhealthy.
    missing = tmp_path / "nope" / "arena.db"
    assert subprocess.run([sys.executable, "-m", "arena.main", "health", "--db", str(missing)],
                          capture_output=True).returncode == 1


def test_holdings_show_the_latest_mark_not_a_stale_close(tmp_path):
    """Regression: the dashboard froze when a daily job failed, and on an
    execution day showed the previous close until 16:45."""
    import json as _json

    import pandas as pd

    from arena import ledger, prices
    from arena.api import queries
    from arena.ledger import SlotTarget
    conn = connect(tmp_path / "a.db")
    df = _market()
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(conn, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    d = "2024-04-01"
    prev_close = conn.execute("SELECT close FROM prices WHERE ticker='MSFT' AND date<? ORDER BY date DESC LIMIT 1",
                              (d,)).fetchone()[0]
    conn.execute("DELETE FROM prices WHERE date >= ?", (d,))           # morning: no daily bar for today yet
    ledger.create_agent(conn, "claude", 1000, "2024-03-28")
    conn.execute("INSERT INTO weeks(week_no, decision_date, exec_date, status) VALUES (1, '2024-03-28', ?, 'decided')", (d,))
    conn.execute("INSERT INTO pending_orders(agent_id, created_date, exec_date, kind, payload) VALUES "
                 "('claude', '2024-03-28', ?, 'rebalance', ?)", (d, _json.dumps([SlotTarget(1, 1.0, {"MSFT": 0.25}).__dict__])))
    prices.store_opening_prices(conn, pd.DataFrame({"ticker": ["SPY", "MSFT"], "open": [500.0, 400.0]}), d, "test")
    league.run_open(conn, d)

    h = queries.agent_detail(conn, "claude")["holdings"][0]
    assert (h["last"], queries.display_mark(conn)) == (400.0, (d, "open"))     # today's open, not prev_close
    assert h["last"] != prev_close and not h["price_stale"]
    assert queries.scoreboard(conn)["as_of_field"] == "open"

    # After the daily job stores today's bar, the close is shown.
    conn.execute("INSERT INTO prices VALUES ('MSFT', ?, 400, 420, 395, 410, 1e6), ('SPY', ?, 500, 505, 499, 503, 1e6)", (d, d))
    h = queries.agent_detail(conn, "claude")["holdings"][0]
    assert h["last"] == 410.0 and queries.display_mark(conn) == (d, "close")

    # Prices for a later day without a daily snapshot (the daily job failed
    # after downloading prices) still move the dashboard forward.
    conn.execute("INSERT INTO prices VALUES ('MSFT', '2024-04-02', 410, 430, 405, 425, 1e6), ('SPY', '2024-04-02', 503, 510, 502, 508, 1e6)")
    assert queries.agent_detail(conn, "claude")["holdings"][0]["last"] == 425.0
