"""Phase 5: the adaptive layer. BACKTEST ONLY: nothing here is wired into
the live league (going live needs 4+ weeks of shadow decisions and a human's
approval). Settings in config/lab.yaml `adaptive:` (human-edited).

The market changes fast, strategy rules rarely. Three speeds:

  5A  inside each strategy, at every rebalance (shared helpers, the engine
      applies them; Options.vol_sizing / Options.atr_stop_multiple):
        vol_scaled()     each position's weight ~ 1 / its 20-day volatility,
                         same total, so each carries similar risk; the hard
                         limits are applied after (engine.limit_targets)
        atr_stop_pct()   stop distance = multiple x ATR(14) / price, between
                         min_stop_pct and max_stop_pct; an agent may only pick
                         the multiple inside multiple_range
  5B  across an agent's strategies, weekly: allocate()
        weight ~ equal x (1 + tilt x standardized score), where score is each
        strategy's risk-adjusted return (annualized mean / std) on past days
        in the CURRENT market state, and tilt shrinks with thin evidence
        (days / (days + shrink_days)). Clipped to [min_weight, max_weight],
        at most max_change per rebalance. Weekly, plus an extra move only
        when the market state changed and held confirm_days days.
  5C  the whole portfolio, daily: RiskDial
        exposure cut to market_exposure when SPY is below its 200-day average
        or volatility is high, and to the drawdown levels when the portfolio
        is down from its peak; it comes back at most recover_per_week a week.
        It can only REDUCE exposure (never above 100%).

No lookahead: the weights and the exposure decided at day t's close apply to
day t+1's returns; every input is data up to t.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import config as labconfig


def cfg():
    return labconfig.get().adaptive


# ==========================================================================
# 5A: volatility-aware sizing and stops
# ==========================================================================

def realized_vol(close: pd.DataFrame, days: int | None = None) -> pd.DataFrame:
    """Annualized close-to-close volatility, per ticker, as of each day."""
    days = days or cfg().vol_days
    return close.pct_change(fill_method=None).rolling(days, min_periods=days).std() * math.sqrt(252)


def atr(frame: pd.DataFrame, days: int | None = None) -> pd.DataFrame:
    """Average true range, per ticker, as of each day."""
    days = days or cfg().atr_days
    h, l, c = frame["high"], frame["low"], frame["close"]
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()]).groupby(level=0).max()
    return tr.rolling(days, min_periods=days).mean()


def vol_scaled(weights: dict[str, float], vols: dict[str, float]) -> dict[str, float]:
    """Same total |weight|, each position sized ~ 1/volatility (sign kept).
    Positions without a volatility keep their weight."""
    known = {t: v for t, v in vols.items() if t in weights and v is not None and math.isfinite(v) and v > 0}
    if len(known) < 2:
        return dict(weights)
    gross = sum(abs(weights[t]) for t in known)
    inv = {t: 1 / v for t, v in known.items()}
    k = gross / sum(inv.values())
    return {t: (math.copysign(inv[t] * k, w) if t in known else w) for t, w in weights.items()}


def atr_stop_pct(atr_value: float, price: float, multiple: float | None = None) -> float | None:
    """The stop's distance as a fraction of the price, from volatility."""
    c = cfg()
    m = c.atr_multiple if multiple is None else min(max(multiple, c.atr_multiple_range[0]), c.atr_multiple_range[1])
    if not (atr_value and price and math.isfinite(atr_value) and math.isfinite(price) and price > 0):
        return None
    return min(max(m * atr_value / price, c.min_stop_pct), c.max_stop_pct)


# ==========================================================================
# 5B: the regime allocator
# ==========================================================================

def _clip_normalize(w: np.ndarray, lo: float, hi: float, cap_lo=None, cap_hi=None) -> np.ndarray:
    """Weights summing to 1 inside [lo, hi] (and optional per-item bounds)."""
    lo_v = np.maximum(lo, cap_lo) if cap_lo is not None else np.full_like(w, lo)
    hi_v = np.minimum(hi, cap_hi) if cap_hi is not None else np.full_like(w, hi)
    hi_v = np.maximum(hi_v, lo_v)
    x = np.clip(w, lo_v, hi_v)
    for _ in range(100):
        gap = 1 - x.sum()
        if abs(gap) < 1e-12:
            break
        free = (x < hi_v - 1e-12) if gap > 0 else (x > lo_v + 1e-12)
        if not free.any():
            break
        x[free] += gap / free.sum()
        x = np.clip(x, lo_v, hi_v)
    return x


def state_series(index: pd.DatetimeIndex, labels: dict[str, str | None]) -> pd.Series:
    """For each day, the market state at the close BEFORE it (decision time)."""
    days = [d.strftime("%Y-%m-%d") for d in index]
    return pd.Series([labels.get(days[i - 1]) if i else None for i in range(len(days))], index=index)


def allocate(returns: pd.DataFrame, states: pd.Series, decision_day: str, prev: np.ndarray | None,
             labels: dict[str, str | None]) -> tuple[np.ndarray, dict]:
    """Weights per strategy (columns of `returns`) for the days after
    decision_day, using only returns up to and including it."""
    c = cfg()
    n = returns.shape[1]
    equal = np.full(n, 1 / n)
    hist = returns.loc[:decision_day].iloc[-c.lookback_days:]
    state = labels.get(decision_day)
    days_in = 0
    score = np.zeros(n)
    if state:
        mask = (states.reindex(hist.index) == state).to_numpy()
        days_in = int(mask.sum())
        if days_in >= c.min_regime_days:
            sub = hist[mask]
            sd = sub.std().to_numpy()
            score = np.where(sd > 0, sub.mean().to_numpy() / np.where(sd > 0, sd, 1) * math.sqrt(252), 0.0)
    z = (score - score.mean()) / score.std() if score.std() > 0 else np.zeros(n)
    shrink = days_in / (days_in + c.shrink_days) if days_in >= c.min_regime_days else 0.0
    target = _clip_normalize(equal * (1 + c.max_tilt * shrink * z), c.min_weight, c.max_weight)
    if prev is not None:
        target = _clip_normalize(target, c.min_weight, c.max_weight, prev - c.max_change, prev + c.max_change)
    return target, {"state": state, "days_in_state": days_in, "shrink": round(shrink, 3),
                    "scores": [round(float(x), 3) for x in score]}


# ==========================================================================
# 5C: the risk dial
# ==========================================================================

@dataclass
class RiskDial:
    exposure: float = 1.0
    last_raise: pd.Timestamp | None = None
    log: list = field(default_factory=list)

    def target(self, market_inputs: dict | None, drawdown: float) -> tuple[float, list[str]]:
        c = cfg()
        t, why = 1.0, []
        if market_inputs and (market_inputs.get("trend") == "down" or market_inputs.get("vol") == "high"):
            t = min(t, c.market_exposure)
            why.append(f"market {market_inputs.get('trend')}/{market_inputs.get('vol')}")
        for level, exp in c.drawdown_levels:
            if drawdown >= level:
                t = min(t, exp)
                why.append(f"drawdown {drawdown:.1%}")
        return t, why

    def update(self, day: pd.Timestamp, market_inputs: dict | None, drawdown: float) -> float:
        """Cut at once; come back at most recover_per_week per week. Never above 1."""
        t, why = self.target(market_inputs, drawdown)
        if t < self.exposure - 1e-12:
            self.exposure = t
            self.log.append((day, round(t, 3), "cut: " + ", ".join(why)))
        elif t > self.exposure + 1e-12 and (self.last_raise is None or (day - self.last_raise).days >= 7):
            self.exposure = min(t, self.exposure + cfg().recover_per_week)
            self.last_raise = day
            self.log.append((day, round(self.exposure, 3), "recover"))
        return self.exposure


# ==========================================================================
# The portfolio of strategies
# ==========================================================================

@dataclass
class PortfolioResult:
    equity: pd.Series
    weights: pd.DataFrame          # decided at each day's close (applied the next day)
    exposure: pd.Series
    turnover: float
    rebalances: list[str]
    decisions: list[dict]


def simulate(returns: pd.DataFrame, labels: dict[str, str | None], market_inputs: dict[str, dict],
             start: str, end: str, allocator: bool, dial: bool, bps_per_side: float | None = None,
             capital: float = 1000.0) -> PortfolioResult:
    """Combine strategy daily returns (columns) from start to end.
    Equal weight / allocator, with or without the risk dial. Weekly
    rebalance at the week's last close; costs on every change of a
    strategy's dollar share (the trades that moving capital needs)."""
    c = cfg()
    bps = (labconfig.get().costs.bps_per_side if bps_per_side is None else bps_per_side) / 1e4
    r = returns.loc[:end].fillna(0.0)
    states = state_series(r.index, labels)
    days = r.index[(r.index >= pd.Timestamp(start)) & (r.index <= pd.Timestamp(end))]
    n = r.shape[1]
    w = np.full(n, 1 / n)
    dial_ = RiskDial()
    exposure = 1.0
    eq = capital
    peak = capital
    used_state = None
    held_since: dict = {"state": None, "days": 0}
    out_eq, out_w, out_x, rebal, decisions = [], [], [], [], []
    turnover = 0.0
    idx = list(r.index)
    pos = {d: i for i, d in enumerate(idx)}
    for d in days:
        # Today's return on yesterday's decisions; then the weights drift.
        ret = r.loc[d].to_numpy()
        eq *= 1 + exposure * float(np.dot(w, ret))
        w = _drift(w, ret)
        peak = max(peak, eq)
        key = d.strftime("%Y-%m-%d")
        # Decide at today's close for tomorrow.
        nxt = idx[pos[d] + 1] if pos[d] + 1 < len(idx) else None
        week_end = nxt is None or nxt.isocalendar()[:2] != d.isocalendar()[:2]
        st = labels.get(key)
        if st == held_since["state"]:
            held_since["days"] += 1
        else:
            held_since = {"state": st, "days": 1}
        confirmed_change = (st is not None and st != used_state and held_since["days"] >= c.confirm_days)
        new_w, new_x = w, exposure
        if allocator and (week_end or confirmed_change):
            new_w, info = allocate(r, states, key, w, labels)
            used_state = st
            rebal.append(key)
            decisions.append({"day": key, "why": "weekly" if week_end else "state change held", **info,
                              "weights": [round(float(x), 4) for x in new_w]})
        elif not allocator and week_end:
            new_w = np.full(n, 1 / n)       # equal weight, rebalanced weekly
        if dial:
            new_x = dial_.update(d, market_inputs.get(key), 1 - eq / peak)
        change = float(np.abs(new_x * new_w - exposure * w).sum())
        if change > 1e-9:
            eq *= 1 - change * bps
            turnover += change
        w, exposure = new_w, new_x
        out_eq.append(eq)
        out_w.append(w.copy())
        out_x.append(exposure)
    return PortfolioResult(pd.Series(out_eq, index=days), pd.DataFrame(out_w, index=days, columns=r.columns),
                           pd.Series(out_x, index=days), turnover, rebal, decisions)


def _drift(w: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Weights after a day's returns, before rebalancing."""
    g = w * (1 + r)
    s = g.sum()
    return g / s if s > 0 else w
