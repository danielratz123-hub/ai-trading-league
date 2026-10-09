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
    assert r.status_code == 200 and r.json() == {"paused": True}
    assert c.get("/api/status").json()["paused"] is True
    conn = connect(path)
    with pytest.raises(league.LeagueError, match="paused"):
        league.execute(conn)
    conn.close()
    assert c.post("/api/admin/resume", headers={"Authorization": "Bearer s3cret"}).json() == {"paused": False}
    mp.delenv("ADMIN_TOKEN")


def test_dashboard_page_is_served(client):
    c, _, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "AI Trading League" in r.text
    assert c.get("/app.js").status_code == 200
