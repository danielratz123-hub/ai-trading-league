"""Market regime classifier: simple, documented rules, no LLM.

Inputs (all as of one trading day's close):
  * SPY's close, its SMA_fast (50) and SMA_slow (200)
  * SPY's realized volatility over the last N (20) days, annualized
  * the VIX close (may be missing: then only realized volatility counts)

Rules, first match wins (thresholds in config/learning.yaml, `regime:`):
  1. high_vol    VIX >= high_vol_vix (30), or realized vol >= high_vol_realized (30%)
  2. trend_up    close > SMA50 > SMA200
  3. trend_down  close < SMA50 < SMA200
  4. choppy      anything else (price between or across the averages)

With fewer than SMA_slow closes there is no label (None).

A regime "holds" for as many consecutive trading days as the label stayed
the same. Capital may be tilted between strategies only once a NEW regime
has held for `confirm_days`, and only once per regime run.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from .config import Regime as RegimeCfg

LABELS = ("trend_up", "trend_down", "choppy", "high_vol")


def realized_vol(closes: Sequence[float], days: int) -> float | None:
    """Annualized standard deviation of the last `days` daily returns."""
    if len(closes) < days + 1:
        return None
    window = closes[-(days + 1):]
    rets = [window[i] / window[i - 1] - 1 for i in range(1, len(window))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def classify(spy_closes: Sequence[float], vix: float | None, cfg: RegimeCfg) -> tuple[str | None, dict]:
    """(label, the numbers used). spy_closes: oldest first, ending at the day classified."""
    closes = [float(x) for x in spy_closes if x is not None]
    if len(closes) < cfg.sma_slow:
        return None, {"reason": f"need {cfg.sma_slow} closes, have {len(closes)}"}
    close = closes[-1]
    fast = sum(closes[-cfg.sma_fast:]) / cfg.sma_fast
    slow = sum(closes[-cfg.sma_slow:]) / cfg.sma_slow
    rv = realized_vol(closes, cfg.realized_vol_days)
    inputs = {"spy_close": round(close, 4), f"sma_{cfg.sma_fast}": round(fast, 4),
              f"sma_{cfg.sma_slow}": round(slow, 4),
              "realized_vol": None if rv is None else round(rv, 4),
              "vix": None if vix is None else round(float(vix), 2)}
    if (vix is not None and vix >= cfg.high_vol_vix) or (rv is not None and rv >= cfg.high_vol_realized):
        label = "high_vol"
    elif close > fast > slow:
        label = "trend_up"
    elif close < fast < slow:
        label = "trend_down"
    else:
        label = "choppy"
    return label, inputs


@dataclass(frozen=True)
class RegimeRun:
    label: str | None
    days_held: int          # consecutive trading days with this label, including the last
    started: str | None     # first day of the run


def current_run(history: Sequence[tuple[str, str | None]]) -> RegimeRun:
    """history: (day, label) oldest first, one per trading day."""
    if not history:
        return RegimeRun(None, 0, None)
    label = history[-1][1]
    n, start = 0, None
    for day, lab in reversed(history):
        if lab != label:
            break
        n += 1
        start = day
    return RegimeRun(label, n, start)


def tilt_allowed(history: Sequence[tuple[str, str | None]], confirm_days: int,
                 last_tilt_day: str | None) -> tuple[bool, str]:
    """May the agent tilt capital now? Only after a regime CHANGE that has
    held for confirm_days, and not twice within the same regime run."""
    run = current_run(history)
    if run.label is None:
        return False, "no regime label yet"
    if run.days_held == len(history):
        # Never saw a different regime before this run: no change happened.
        return False, f"no regime change seen (still {run.label} since the start of the history)"
    if run.days_held < confirm_days:
        return False, (f"regime changed to {run.label} on {run.started}, held {run.days_held} day(s); "
                       f"needs {confirm_days}")
    if last_tilt_day is not None and run.started is not None and last_tilt_day >= run.started:
        return False, f"already tilted on {last_tilt_day} during this {run.label} run"
    return True, f"{run.label} since {run.started} ({run.days_held} days)"
