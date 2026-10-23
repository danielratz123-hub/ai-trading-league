"""Pure daily performance metrics, using the lab's percentage/252-day conventions.

Equity paths include their starting capital as the first observation. Cash
flows must be removed by the caller. Trade P&Ls are dollars, net of costs.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import mean, stdev
from collections.abc import Sequence


def metrics(equity: Sequence[float], pnls: Sequence[float] = ()) -> dict:
    """Mirror lab metrics: sample std, zero risk-free rate, positive drawdown.

    Sortino uses sqrt(mean(min(return, 0)**2)) over ALL daily returns.
    Undefined ratios use 0 (as Sharpe does in the lab); missing equity and
    trade averages use None. Breakeven trades count in win rate's denominator.
    """
    if any(not math.isfinite(v) for v in [*equity, *pnls]):
        raise ValueError("Equity and P&Ls must be finite")
    daily = [b / a - 1 for a, b in zip(equity, equity[1:]) if a > 0]
    sd = stdev(daily) if len(daily) > 1 else 0.0
    downside = math.sqrt(mean(min(r, 0) ** 2 for r in daily)) if daily else 0.0
    avg = mean(daily) if daily else 0.0
    peak, drawdown = 0.0, 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            drawdown = max(drawdown, 1 - value / peak)
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    return {
        "total_return_pct": round((equity[-1] / equity[0] - 1) * 100, 3)
        if equity and equity[0] > 0 else None,
        "sharpe": round(avg / sd * math.sqrt(252), 3) if sd > 0 else 0.0,
        "sortino": round(avg / downside * math.sqrt(252), 3) if downside > 0 else 0.0,
        "max_drawdown_pct": round(drawdown * 100, 3) if equity else None,
        "trades": len(pnls),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if pnls else None,
        "avg_win": round(mean(wins), 3) if wins else None,
        "avg_loss": round(mean(losses), 3) if losses else None,
    }


def window_stats(equity: Sequence[tuple[str, float]], trades: Sequence[tuple[str, float]] = (),
                 *, as_of: str | None = None, initial_equity: float | None = None) -> dict:
    """Calendar week (Monday), month and all-time, ending at as_of/latest day.

    Keep the last point on duplicate dates. Use the preceding close as each
    window's baseline so Monday/the first of the month contributes a return.
    initial_equity optionally supplies capital before the first daily point.
    Trade windows use the closing date. No dependence on today's clock.
    """
    points = sorted(dict(equity).items())
    end = as_of or (points[-1][0] if points else None)
    day = date.fromisoformat(end) if end else None
    starts = {"weekly": (day - timedelta(days=day.weekday())).isoformat() if day else None,
              "monthly": day.replace(day=1).isoformat() if day else None, "all_time": None}
    result = {}
    for name, start in starts.items():
        selected = [(d, v) for d, v in points if end and d <= end and (not start or d >= start)]
        before = [v for d, v in points if start and d < start]
        baseline = before[-1] if before else initial_equity
        values = ([baseline] if baseline is not None and selected else []) + [v for _, v in selected]
        pnls = [p for d, p in trades if end and d <= end and (not start or d >= start)]
        result[name] = {"start": start or (selected[0][0] if selected else None), "end": end,
                        **metrics(values, pnls),
                        "equity_curve": [{"date": d, "equity": v} for d, v in selected]}
    return result
