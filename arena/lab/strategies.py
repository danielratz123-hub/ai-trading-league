"""Live strategies written as code, plus the benchmark.

The league's live strategies are "manual": each Saturday the agent picks the
positions by following its strategy's written rules. A backtester can only
replay code, so the rules are written down here as target_weights(prices,
date) functions (the code-slot interface) and checked against what the
agent actually picked (repro.py). Where the written rules leave room for
judgement, the choices made here are listed in the docstring.

Every function only reads `prices`, which the engine cuts at `date`.
"""
from __future__ import annotations

import pandas as pd

from ..universe import SECTOR_ETF, SECTOR_OF, SP500


def spy_buy_and_hold(prices: pd.DataFrame, date: str) -> dict:
    """The benchmark: 100% SPY."""
    return {"SPY": 1.0}


def _stocks(close: pd.DataFrame) -> list[str]:
    return [t for t in close.columns if t in set(SP500)]


def _most_traded(prices: pd.DataFrame, names: list[str], n: int) -> list[str]:
    """The n names with the highest 20-day average dollar volume."""
    dv = (prices["close"][names] * prices["volume"][names]).iloc[-20:].mean()
    return dv.dropna().sort_values(ascending=False).head(n).index.tolist()


def _ret(close: pd.DataFrame, days: int) -> pd.Series:
    """Return over the last `days` trading days (close to close)."""
    if len(close) <= days:
        return pd.Series(dtype=float)
    return close.iloc[-1] / close.iloc[-1 - days] - 1


def gpt_six_month_momentum(prices: pd.DataFrame, date: str, top: int = 4, cap: float | None = 2.5) -> dict:
    """GPT slot 1, "Capped six-month momentum leaders" (v1 had no cap).

    Written rule: the four strongest S&P 500 stocks by 126-trading-day
    return while above their 200-day averages, excluding moves above 250%;
    equal weight. Choices: S&P 500 stocks only (no ETFs); "126-day return" =
    close today / close 126 trading days ago - 1; a stock needs 200 closes."""
    close = prices["close"][_stocks(prices["close"])]
    if len(close) < 200:
        return {}
    last = close.iloc[-1]
    sma200 = close.iloc[-200:].mean()
    r126 = _ret(close, 126)
    ok = last.notna() & sma200.notna() & r126.notna() & (last > sma200)
    if cap is not None:
        ok &= r126 < cap
    picks = r126[ok].sort_values(ascending=False).head(top)
    return {t: 1.0 / top for t in picks.index}


def gpt_six_month_momentum_uncapped(prices: pd.DataFrame, date: str) -> dict:
    """GPT slot 1 v1 "Six-month momentum leaders" (week 1, before the 250% cap)."""
    return gpt_six_month_momentum(prices, date, cap=None)


def claude_momentum_leaders(prices: pd.DataFrame, date: str, top: int = 9, pool: int = 100) -> dict:
    """Claude slot 1 v1, "Tech/AI + refiner momentum" (weeks 1-2, retired).

    Written rule: "leading momentum names above 50dma with strong 1m/3m
    returns". Choices: the 100 most traded S&P 500 stocks (the agent picks
    large, liquid names), above their 50-day average, ranked by the mean of
    their 21- and 63-day return ranks; top 9, equal weight. The agent also
    used judgement (sector themes, an ETF), so expect a looser match."""
    close = prices["close"][_most_traded(prices, _stocks(prices["close"]), pool)]
    if len(close) < 64:
        return {}
    last = close.iloc[-1]
    above = last > close.iloc[-50:].mean()
    score = (_ret(close, 21).rank(pct=True) + _ret(close, 63).rank(pct=True)) / 2
    picks = score[above & score.notna()].sort_values(ascending=False).head(top)
    return {t: 1.0 / top for t in picks.index}


def claude_sector_rotation(prices: pd.DataFrame, date: str, sectors: int = 4, names: int = 7,
                           pool: int = 100) -> dict:
    """Claude slot 1, "Sector-rotation momentum" (from week 3).

    Written rule: "rotate into the sectors leading on 1-week, 1-month and
    3-month returns ... 7 concentrated names". Choices: sectors ranked by
    their sector ETF's mean 5/21/63-day return; the top 4 sectors; inside
    them, the 100 most traded S&P 500 stocks above their 50-day average,
    ranked by the same mean; the 7
    best names taken round-robin across the sectors (2, 2, 2, 1)."""
    close = prices["close"]
    if len(close) < 64:
        return {}

    def mom(cols):
        c = close[cols]
        return (_ret(c, 5) + _ret(c, 21) + _ret(c, 63)) / 3

    etfs = [e for e in SECTOR_ETF.values() if e in close.columns]
    by_etf = mom(etfs).dropna().sort_values(ascending=False)
    etf_sector = {e: s for s, e in SECTOR_ETF.items()}
    leaders = [etf_sector[e] for e in by_etf.index[:sectors]]
    stocks = _most_traded(prices, _stocks(close), pool)
    last = close[stocks].iloc[-1]
    above = last > close[stocks].iloc[-50:].mean()
    score = mom(stocks)[above].dropna()
    queues = [score[[t for t in score.index if SECTOR_OF.get(t) == s]].sort_values(ascending=False).index.tolist()
              for s in leaders]
    picks: list[str] = []
    while len(picks) < names and any(queues):
        for q in queues:
            if q and len(picks) < names:
                picks.append(q.pop(0))
    return {t: 1.0 / names for t in picks}


# name -> (function, the live strategy it encodes)
LIBRARY = {
    "spy_buy_and_hold": (spy_buy_and_hold, "benchmark"),
    "gpt_six_month_momentum": (gpt_six_month_momentum, "gpt slot 1 'Capped six-month momentum leaders'"),
    "gpt_six_month_momentum_uncapped": (gpt_six_month_momentum_uncapped, "gpt slot 1 v1 'Six-month momentum leaders'"),
    "claude_momentum_leaders": (claude_momentum_leaders, "claude slot 1 v1 'Tech/AI + refiner momentum'"),
    "claude_sector_rotation": (claude_sector_rotation, "claude slot 1 'Sector-rotation momentum'"),
}
