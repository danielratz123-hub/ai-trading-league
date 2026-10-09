"""Historical simulation of a weekly-rebalanced portfolio, with the league's
real cost model (commission, slippage, borrow fee).

Timing mirrors the live league: weights are decided with data up to the
last close of the previous week, and trades fill at the open of the first
trading day of the new week.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import costs
from .ledger import SlotTarget
from .risk import validate_portfolio


@dataclass
class BacktestResult:
    start: str
    end: str
    weeks: int
    total_return: float
    annualized_return: float
    annualized_vol: float
    sharpe: float
    max_drawdown: float
    turnover_annualized: float
    commissions: float
    slippage: float
    borrow_fees: float
    spy_return: float | None
    best_week: float
    worst_week: float
    limit_warnings: list[str]

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        for k, v in d.items():
            if isinstance(v, float):
                d[k] = round(float(v), 4) if np.isfinite(v) else None
        return d


def weekly_schedule(index: pd.DatetimeIndex, start: str, end: str) -> list[tuple[str, str]]:
    """[(decision_date, exec_date)]: exec = first trading day of each week
    in [start, end]; decision = the trading day just before it."""
    days = index[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]
    out = []
    for i, d in enumerate(days):
        prev = index[index < d]
        if len(prev) == 0:
            continue
        is_first_of_week = i == 0 or d.isocalendar()[:2] != days[i - 1].isocalendar()[:2]
        if is_first_of_week:
            out.append((prev[-1].strftime("%Y-%m-%d"), d.strftime("%Y-%m-%d")))
    return out


def simulate(opens: pd.DataFrame, closes: pd.DataFrame,
             weights_by_exec_date: dict[str, dict[str, float]],
             start: str, end: str, capital: float = 1000.0) -> BacktestResult:
    exec_dates = sorted(weights_by_exec_date)
    if not exec_dates:
        raise ValueError("No rebalance dates in range.")
    closes = closes.ffill()   # a missing bar is valued at the last close
    days = closes.index[(closes.index >= pd.Timestamp(exec_dates[0])) & (closes.index <= pd.Timestamp(end))]

    cash, shares = capital, {}
    paid_through: dict[str, pd.Timestamp] = {}
    commissions = slippage = borrow = traded = 0.0
    equity_curve = []
    for d in days:
        key = d.strftime("%Y-%m-%d")
        if key in weights_by_exec_date:
            o = opens.loc[d]
            eq_open = cash + sum(sh * (o.get(t) if pd.notna(o.get(t)) else closes.loc[d, t])
                                 for t, sh in shares.items())
            target = weights_by_exec_date[key]
            orders = []
            for t in sorted(set(shares) | set(target)):
                px = o.get(t, np.nan)
                if pd.isna(px):
                    continue  # can't trade without an open price
                delta = target.get(t, 0.0) * eq_open / px - shares.get(t, 0.0)
                if not costs.is_too_small(delta, px):
                    orders.append((delta > 0, t, delta, px))
            for _, t, delta, px in sorted(orders):  # sells/shorts first
                fill = costs.fill_price(px, delta)
                fee = costs.commission(delta, fill)
                old = shares.get(t, 0.0)
                cash -= delta * fill + fee
                commissions += fee
                slippage += abs(delta) * abs(fill - px)
                traded += abs(delta) * fill
                new = old + delta
                shares[t] = new
                if new < 0 and old >= 0:
                    paid_through[t] = d - pd.Timedelta(days=1)
                if abs(new) < 1e-9:
                    shares.pop(t)
                    paid_through.pop(t, None)
        c = closes.loc[d]
        for t, sh in shares.items():
            if sh < 0:
                days_owed = (d - paid_through[t]).days
                fee = costs.borrow_fee(-sh * c[t], days_owed)
                cash -= fee
                borrow += fee
                paid_through[t] = d
        equity_curve.append(cash + sum(sh * c[t] for t, sh in shares.items()))

    eq = pd.Series(equity_curve, index=days)
    daily = eq.pct_change().dropna()
    years = max(len(eq) / 252, 1e-9)
    total = eq.iloc[-1] / capital - 1
    weekly = eq.resample("W-FRI").last().pct_change().dropna()
    spy = None
    if "SPY" in closes:
        spy_open = opens.loc[days[0], "SPY"]
        spy = float(closes.loc[days[-1], "SPY"] / spy_open - 1) if pd.notna(spy_open) else None

    # Group identical problems so a fixed portfolio doesn't repeat one
    # message for every week.
    problems: dict[str, list[str]] = {}
    for d, w in sorted(weights_by_exec_date.items()):
        res = validate_portfolio([SlotTarget(1, 1.0, w)])
        if not res.ok:
            problems.setdefault("; ".join(res.errors), []).append(d)
    warnings = [f"{len(ds)} week(s), first {ds[0]}: {msg}" for msg, ds in problems.items()]
    return BacktestResult(
        start=days[0].strftime("%Y-%m-%d"), end=days[-1].strftime("%Y-%m-%d"),
        weeks=len(exec_dates), total_return=total,
        annualized_return=(1 + total) ** (1 / years) - 1 if total > -1 else -1.0,
        annualized_vol=float(daily.std() * np.sqrt(252)) if len(daily) > 1 else 0.0,
        sharpe=float(daily.mean() / daily.std() * np.sqrt(252)) if len(daily) > 1 and daily.std() > 0 else 0.0,
        max_drawdown=float((eq / eq.cummax() - 1).min()),
        turnover_annualized=traded / float(eq.mean()) / years,
        commissions=commissions, slippage=slippage, borrow_fees=borrow, spy_return=spy,
        best_week=float(weekly.max()) if len(weekly) else 0.0,
        worst_week=float(weekly.min()) if len(weekly) else 0.0,
        # The real limits apply to the whole portfolio; here we report what
        # would break them if this strategy were 100% of the account.
        limit_warnings=warnings[:5] + ([f"... and {len(warnings) - 5} more"] if len(warnings) > 5 else []),
    )
