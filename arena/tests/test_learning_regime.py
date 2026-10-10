"""Regime classifier (pure rules) and the tilt confirmation rule."""
from __future__ import annotations

from arena.learning import config as lconfig
from arena.learning.regime import classify, current_run, realized_vol, tilt_allowed

CFG = lconfig.get().regime


def series(start: float, daily: float, n: int = 260, wiggle: float = 0.0) -> list[float]:
    out, p = [], start
    for i in range(n):
        p *= 1 + daily + (wiggle if i % 2 else -wiggle)
        out.append(p)
    return out


def test_trend_up():
    label, inputs = classify(series(100, 0.001), vix=15, cfg=CFG)
    assert label == "trend_up"
    assert inputs["spy_close"] > inputs["sma_50"] > inputs["sma_200"]


def test_trend_down():
    assert classify(series(100, -0.001), vix=18, cfg=CFG)[0] == "trend_down"


def test_choppy_when_price_between_averages():
    up = series(100, 0.002, 200)
    closes = up + [up[-1] * (1 - 0.003 * i) for i in range(1, 40)]   # sharp pullback below SMA50, above SMA200
    label, inputs = classify(closes, vix=18, cfg=CFG)
    assert inputs["spy_close"] < inputs["sma_50"] and inputs["sma_50"] > inputs["sma_200"]
    assert label == "choppy"


def test_high_vol_from_vix_beats_trend():
    assert classify(series(100, 0.001), vix=CFG.high_vol_vix, cfg=CFG)[0] == "high_vol"
    assert classify(series(100, 0.001), vix=CFG.high_vol_vix - 0.01, cfg=CFG)[0] == "trend_up"


def test_high_vol_from_realized_vol_without_vix():
    wild = series(100, 0.0005, wiggle=0.03)          # ~48% annualized
    assert realized_vol(wild, 20) > CFG.high_vol_realized
    assert classify(wild, vix=None, cfg=CFG)[0] == "high_vol"


def test_not_enough_history():
    label, inputs = classify(series(100, 0.001, 150), vix=15, cfg=CFG)
    assert label is None and "need" in inputs["reason"]


def test_current_run():
    h = [("d1", "choppy"), ("d2", "trend_up"), ("d3", "trend_up")]
    r = current_run(h)
    assert (r.label, r.days_held, r.started) == ("trend_up", 2, "d2")
    assert current_run([]).label is None


def test_tilt_needs_a_change_held_for_confirm_days():
    assert CFG.confirm_days == 2
    ok, why = tilt_allowed([("d1", "choppy"), ("d2", "trend_up")], 2, None)
    assert not ok and "held 1 day" in why
    ok, _ = tilt_allowed([("d1", "choppy"), ("d2", "trend_up"), ("d3", "trend_up")], 2, None)
    assert ok
    # No change ever seen: nothing to react to.
    ok, why = tilt_allowed([("d1", "trend_up"), ("d2", "trend_up"), ("d3", "trend_up")], 2, None)
    assert not ok and "no regime change" in why


def test_only_one_tilt_per_regime_run():
    h = [("d1", "choppy"), ("d2", "trend_up"), ("d3", "trend_up"), ("d4", "trend_up")]
    assert tilt_allowed(h, 2, "d1")[0]          # tilted in the previous regime: fine
    ok, why = tilt_allowed(h, 2, "d3")
    assert not ok and "already tilted" in why
