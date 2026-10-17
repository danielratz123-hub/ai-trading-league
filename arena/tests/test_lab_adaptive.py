"""Backtest lab, phase 5: the adaptive layer (backtest only)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from arena.lab import adaptive, config as labconfig, engine
from arena.tests.test_lab_engine import FRAME, UNI

C = labconfig.get().adaptive


def test_vol_scaling_keeps_the_total_and_equalizes_risk():
    w = adaptive.vol_scaled({"A": 0.2, "B": 0.2, "C": -0.1}, {"A": 0.10, "B": 0.40, "C": 0.20})
    assert sum(abs(x) for x in w.values()) == pytest.approx(0.5)
    assert w["A"] * 0.10 == pytest.approx(w["B"] * 0.40) == pytest.approx(-w["C"] * 0.20)
    assert w["C"] < 0
    assert adaptive.vol_scaled({"A": 0.2}, {"A": 0.3}) == {"A": 0.2}         # one name: nothing to balance


def test_atr_stops_stay_inside_the_band_and_the_multiple_range():
    assert adaptive.atr_stop_pct(2.0, 100.0) == pytest.approx(C.atr_multiple * 0.02)
    assert adaptive.atr_stop_pct(0.1, 100.0) == C.min_stop_pct
    assert adaptive.atr_stop_pct(20.0, 100.0) == C.max_stop_pct
    assert adaptive.atr_stop_pct(2.0, 100.0, multiple=99) == pytest.approx(C.atr_multiple_range[1] * 0.02)
    assert adaptive.atr_stop_pct(float("nan"), 100.0) is None


def test_engine_applies_5a_and_still_caps_positions():
    spec = engine.StrategySpec("two", fn=lambda p, d: {"AAPL": 0.2, "F": 0.2})
    r = engine.run(spec, FRAME, "2017-01-02", "2017-06-30", options=engine.Options(universe=UNI, vol_sizing=True,
                                                                                   atr_stop_multiple=2.5))
    plain = engine.run(spec, FRAME, "2017-01-02", "2017-06-30", options=engine.Options(universe=UNI))
    assert r.metrics["total_return_pct"] != plain.metrics["total_return_pct"]
    assert any("capped" in n for n in r.notes) or r.trades          # the hard cap still runs after sizing


def _returns(n_days=600, seed=3):
    idx = pd.bdate_range("2018-01-01", periods=n_days)
    rng = np.random.default_rng(seed)
    a = rng.normal(0.001, 0.01, n_days)      # good in every state
    b = rng.normal(-0.0005, 0.01, n_days)    # bad
    c = rng.normal(0.0, 0.01, n_days)
    return pd.DataFrame({"good": a, "bad": b, "flat": c}, index=idx)


def test_allocator_tilts_to_the_better_strategy_within_bounds():
    r = _returns()
    labels = {d.strftime("%Y-%m-%d"): "up_low" for d in r.index}
    states = adaptive.state_series(r.index, labels)
    day = r.index[-1].strftime("%Y-%m-%d")
    w, info = adaptive.allocate(r, states, day, None, labels)
    assert w.sum() == pytest.approx(1) and w.min() >= C.min_weight - 1e-9 and w.max() <= C.max_weight + 1e-9
    assert w[0] > 1 / 3 > w[1] and info["days_in_state"] > 0
    prev = np.full(3, 1 / 3)
    w2, _ = adaptive.allocate(r, states, day, prev, labels)
    assert np.abs(w2 - prev).max() <= C.max_change + 1e-9


def test_allocator_uses_only_the_past():
    r = _returns()
    labels = {d.strftime("%Y-%m-%d"): "up_low" for d in r.index}
    states = adaptive.state_series(r.index, labels)
    day = r.index[400].strftime("%Y-%m-%d")
    w1, _ = adaptive.allocate(r, states, day, None, labels)
    r2 = r.copy()
    r2.iloc[401:] = r2.iloc[401:] * -10           # the future changes completely
    w2, _ = adaptive.allocate(r2, states, day, None, labels)
    assert np.allclose(w1, w2)


def test_thin_evidence_stays_near_equal_weight():
    r = _returns()
    labels = {d.strftime("%Y-%m-%d"): ("down_high" if i > len(r) - 30 else "up_low") for i, d in enumerate(r.index)}
    states = adaptive.state_series(r.index, labels)
    w, info = adaptive.allocate(r, states, r.index[-1].strftime("%Y-%m-%d"), None, labels)
    assert info["state"] == "down_high" and info["shrink"] < 0.15
    assert np.abs(w - 1 / 3).max() < 0.1


def test_risk_dial_cuts_at_once_and_recovers_slowly():
    d = adaptive.RiskDial()
    day = pd.Timestamp("2020-03-02")
    assert d.update(day, {"trend": "down", "vol": "high"}, 0.0) == C.market_exposure
    assert d.update(day + pd.Timedelta(days=1), {"trend": "down", "vol": "high"}, 0.13) == C.drawdown_levels[1][1]
    x = d.update(day + pd.Timedelta(days=2), {"trend": "up", "vol": "low"}, 0.0)
    assert x == pytest.approx(C.drawdown_levels[1][1] + C.recover_per_week)
    assert d.update(day + pd.Timedelta(days=3), {"trend": "up", "vol": "low"}, 0.0) == x     # not again this week
    assert d.update(day + pd.Timedelta(days=10), {"trend": "up", "vol": "low"}, 0.0) <= 1.0


def test_simulate_equal_weight_matches_the_average_and_dial_reduces_exposure():
    r = _returns(260)
    labels = {d.strftime("%Y-%m-%d"): "up_low" for d in r.index}
    inputs = {d.strftime("%Y-%m-%d"): {"trend": "down", "vol": "high"} for d in r.index}
    start, end = r.index[0].strftime("%Y-%m-%d"), r.index[-1].strftime("%Y-%m-%d")
    eq = adaptive.simulate(r, labels, {}, start, end, allocator=False, dial=False, bps_per_side=0)
    approx = 1000 * float(np.prod(1 + r.mean(axis=1)))
    assert eq.equity.iloc[-1] == pytest.approx(approx, rel=0.02)       # weekly rebalance ~ the daily average
    dialed = adaptive.simulate(r, labels, inputs, start, end, allocator=False, dial=True, bps_per_side=0)
    assert dialed.exposure.max() <= C.market_exposure + 1e-9
    assert (dialed.exposure <= 1.0).all()
    al = adaptive.simulate(r, labels, {}, start, end, allocator=True, dial=False)
    assert al.weights.sum(axis=1).round(6).eq(1).all() and al.rebalances
