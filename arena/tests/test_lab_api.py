"""Phase 6: the dashboard's backtest lab data (GET /api/lab, read-only)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from arena import ledger
from arena.api import lab as lab_api
from arena.db import connect
from arena.lab import schema
from arena.learning import strategies as strat


def _db(path):
    c = connect(path)
    ledger.create_agent(c, "gpt", 1000.0, "2024-01-02")
    return c


def test_hidden_until_the_lab_tables_exist(tmp_path):
    c = _db(tmp_path / "a.db")
    out = lab_api.overview(c)
    assert out["available"] is False and "Survivorship" in out["caveat"]
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='market_states'").fetchone() is None   # nothing created


def test_overview_has_state_experiments_runs_and_caveat(tmp_path):
    c = _db(tmp_path / "a.db")
    schema.migrate(c)
    c.executemany("INSERT INTO market_states(day, label, inputs, source, computed_at) VALUES (?,?,?,?,?)",
                  [("2026-10-15", "up_low", json.dumps({"spy_close": 700, "sma_200": 650, "realized_vol": 0.1}), "t", "t"),
                   ("2026-10-16", "up_normal", json.dumps({"spy_close": 710, "sma_200": 651, "realized_vol": 0.14}), "t", "t")])
    s = strat.create(c, "gpt", 1, "momo", "rules", None, "initial", "t", 1, "2026-10-01T00:00:00+00:00")
    res = {"start": "2017-01-02", "end": "2025-10-16", "total_return_pct": 120.0, "max_drawdown_pct": 30.0,
           "risk_adjusted_score": 1.1, "period_score": 4.0, "trades": 300, "vs_spy_pct": 20.0,
           "by_state": {"up_low": {"days": 500, "annual_return_pct": 25.0, "trades": 150, "win_rate_pct": 55.0}}}
    c.execute("INSERT INTO backtest_runs(requested_by, idea_key, attempt_no, strategy_name, purpose, status, results, "
              "created_at) VALUES ('agent:gpt', 'top5', 1, 'top5', 'design', 'ok', ?, '2026-10-17T10:00:00+00:00')",
              (json.dumps(res),))
    c.execute("INSERT INTO backtest_runs(requested_by, idea_key, attempt_no, strategy_name, purpose, status, results, "
              "created_at) VALUES ('engine:promotion', 'top5', 2, 'top5', 'walk_forward', 'ok', ?, "
              "'2026-10-17T11:00:00+00:00')", (json.dumps(res),))
    for stage, ok, why in (("proposed", 1, "h"), ("freeze", 1, "ok"), ("walk_forward", 1, "<b>beat</b>"),
                           ("shadow_started", 1, "from 2026-10-19")):
        c.execute("INSERT INTO promotion_log(ts, agent_id, slot, idea_key, stage, passed, reason) VALUES "
                  "('2026-10-17T12:00:00+00:00', 'gpt', 1, 'top5', ?, ?, ?)", (stage, ok, why))
    vid = strat.add_version(c, s.strategy_id, "top5 rules", s.params, "weekly_rewrite", "r", "challenger", 3)
    c.execute("INSERT INTO lab_shadows(version_id, agent_id, slot, champion_version_id, idea_key, start_day, status, "
              "trades, score, champion_score, prob_better, detail, started_at) VALUES (?,?,?,?,?,?,'running',12,0.5,0.2,"
              "0.7,'{\"weeks\": 4}',?)", (vid, "gpt", 1, s.version_id, "top5", "2026-10-19", "2026-10-17T12:00:00+00:00"))
    out = lab_api.overview(c)
    assert out["available"] and out["market_state"]["label"] == "up_normal"
    assert [x["label"] for x in out["state_history"]] == ["up_low", "up_normal"]
    (e,) = out["experiments"]
    assert e["status"] == "shadow" and e["attempts"] == 2 and e["walk_forward"]["trades"] == 300
    assert e["shadow"]["trades"] == 12 and e["shadow"]["min_trades"] >= 1 and e["shadow"]["weeks"] == 4
    assert [s_["stage"] for s_ in e["steps"]][0] == "proposed"
    assert out["recent_runs"][0]["results"]["by_state"]["up_low"]["trades"] == 150
    assert out["risk_dial"]["live"] is False and "Survivorship" in out["caveat"]


def test_http_endpoint_is_read_only(tmp_path, monkeypatch):
    path = tmp_path / "arena.db"
    c = _db(path)
    c.close()
    monkeypatch.setenv("ARENA_DB", str(path))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    import arena.api.app as app_module
    r = TestClient(app_module.create_app()).get("/api/lab")
    assert r.status_code == 200 and r.json()["available"] is False
    c = connect(path)
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='backtest_runs'").fetchone() is None


@pytest.mark.parametrize("page", ["index.html"])
def test_page_has_the_lab_section(page):
    from pathlib import Path
    html = (Path(__file__).parents[1] / "web" / page).read_text(encoding="utf-8")
    for el in ("id=\"lab\"", "lab-exps", "lab-strats", "lab-capital", "lab-caveat-text", "lab.js"):
        assert el in html
