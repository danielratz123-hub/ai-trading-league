"""Event-driven backtest engine (the sandbox).

Runs a strategy through the SAME interface as a live code slot:

    target_weights(prices, date) -> {ticker: weight}

`prices` is the wide frame (field, ticker) the live research context gives a
code slot, cut at `date`; agent code runs in the same sandbox as a live code
slot (arena/sandbox), trusted Python strategies (arena/lab/strategies.py) are
called directly. There is no separate "backtest version" of a strategy.

Timing, per trading day d (no lookahead):
  * decision day (weekly: the week's last trading day; daily: every day):
    after the close, the strategy sees bars up to and including d only.
  * the next trading day's OPEN: the targets are filled (sells first), with
    spread + slippage per side and the league's commission.
  * every day: stop-losses. A long whose low touches its stop exits at the
    stop, or at the open if it gapped through (shorts mirrored). A stopped
    name stays out until the next rebalance, as in the live league.
  * the close: equity is marked; shorts pay the borrow fee (calendar days).

The hard limits (config/learning.yaml hard_limits) apply to every target:
allowed universe only, price >= min_price, 20-day average volume >=
min_avg_daily_volume, each position <= min(position_max_pct,
max_position_pct_of_strategy), shorts <= max_short_exposure_pct, gross <=
max_gross_exposure (no leverage, cash can't go negative), a stop on every
position, and in daily mode at most max_trades_per_strategy_per_day
rebalancing trades a day (stops never count, as live).
"""
from __future__ import annotations

import hashlib
import inspect
import math
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .. import costs as league_costs
from ..config import SETTINGS
from ..learning import config as lconfig
from ..learning.scores import max_drawdown_pct, score as league_score
from . import config as labconfig


class LookaheadError(RuntimeError):
    """A strategy tried to read data after its decision date."""


class StrategyError(RuntimeError):
    """The strategy crashed or returned invalid weights."""


@dataclass(frozen=True)
class StrategySpec:
    name: str
    fn: Callable[[pd.DataFrame, str], dict] | None = None   # trusted Python
    code: str | None = None                                  # agent code: runs in the sandbox
    params: dict = field(default_factory=dict)               # stop_loss_pct, position_max_pct

    def __post_init__(self):
        if (self.fn is None) == (self.code is None):
            raise ValueError("give exactly one of fn or code")

    def source(self) -> str:
        if self.code is not None:
            return self.code
        try:
            return inspect.getsource(self.fn)
        except (OSError, TypeError):
            return repr(self.fn)

    def sha256(self) -> str:
        return hashlib.sha256(self.source().encode()).hexdigest()


@dataclass
class Options:
    """Engine switches. `benchmark=True` (no stops, no per-position cap, no
    price/volume filter; still no leverage) exists only for engine checks
    such as a SPY buy-and-hold, which the live limits would cap at 20%;
    runs.py refuses it for any other purpose, and no agent path sets it."""
    rebalance: str | None = None
    capital: float | None = None
    benchmark: bool = False
    bps_per_side: float | None = None
    universe: set[str] | None = None        # tradable tickers (default: the league universe)


@dataclass
class RunResult:
    equity: pd.Series
    trades: list[dict]
    fills: int
    schedule: list[tuple[str, str]]
    notes: list[str]
    metrics: dict


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------

def schedule(index: pd.DatetimeIndex, start: str, end: str, rebalance: str, min_history: int = 0) -> list[tuple[str, str]]:
    """[(decision_day, exec_day)] with exec days in [start, end]. Weekly =
    exec on the week's first trading day, decided at the close before it
    (the league's rhythm); daily = every day, decided the day before. The
    first decision needs `min_history` bars on or before it."""
    days = [d.strftime("%Y-%m-%d") for d in index]
    out = []
    for i in range(1, len(days)):
        d, prev = days[i], days[i - 1]
        if not start <= d <= end or i < min_history:
            continue
        if rebalance == "daily" or _new_week(prev, d):
            out.append((prev, d))
    return out


def _new_week(prev: str, d: str) -> bool:
    return pd.Timestamp(prev).isocalendar()[:2] != pd.Timestamp(d).isocalendar()[:2]


# --------------------------------------------------------------------------
# Signals
# --------------------------------------------------------------------------

def _future_key(err: KeyError, decision: str) -> bool:
    """Was the missing key a date after the decision date?"""
    for k in err.args:
        try:
            if pd.Timestamp(k) > pd.Timestamp(decision):
                return True
        except (ValueError, TypeError):
            continue
    return False


def _check_weights(raw, universe: set[str], where: str) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise StrategyError(f"{where}: target_weights must return a dict, got {type(raw).__name__}")
    out = {}
    for k, v in raw.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise StrategyError(f"{where}: weight for {k} is not a number") from None
        if not math.isfinite(f):
            raise StrategyError(f"{where}: weight for {k} is not finite")
        k = str(k).upper()
        if k not in universe:
            raise StrategyError(f"{where}: {k} is not in the tradable universe")
        if f:
            out[k] = f
    if sum(abs(x) for x in out.values()) > 1 + 1e-6:
        raise StrategyError(f"{where}: sum of |weights| must be <= 1")
    return out


def signals(spec: StrategySpec, frame: pd.DataFrame, decisions: list[str], universe: set[str]) -> dict[str, dict]:
    """{decision_day: weights}. Each call sees only bars up to its own day."""
    if not decisions:
        return {}
    if spec.code is not None:
        from ..sandbox.runner import PriceDataFile, run_code
        from ..tools.sandbox_tools import BACKTEST_LIMITS
        data = PriceDataFile(frame.loc[:decisions[-1]])
        try:
            r = run_code("strategy_series", spec.code, data, {t: "" for t in sorted(universe)}, decisions[-1],
                         dates=decisions, limits=BACKTEST_LIMITS)
        finally:
            data.cleanup()
        if not r.ok:
            if r.error and "KeyError" in r.error and any(d > decisions[0] for d in decisions):
                raise StrategyError(f"strategy code failed (a KeyError: reading a date it can't see?): {r.error}")
            raise StrategyError(f"strategy code failed: {r.error}")
        return {d: _check_weights(r.series[d], universe, d) for d in decisions}
    out = {}
    for d in decisions:
        view = frame.loc[:d]
        if len(view.index) and view.index.max() > pd.Timestamp(d):      # belt and braces
            raise LookaheadError(f"engine bug: view for {d} ends {view.index.max():%Y-%m-%d}")
        try:
            raw = spec.fn(view, d)
        except KeyError as e:
            if _future_key(e, d):
                raise LookaheadError(f"{spec.name} read data after its decision date {d}: {e}") from None
            raise StrategyError(f"{spec.name} failed on {d}: KeyError {e}") from e
        except (LookaheadError, StrategyError):
            raise
        except Exception as e:  # noqa: BLE001
            raise StrategyError(f"{spec.name} failed on {d}: {type(e).__name__}: {e}") from e
        out[d] = _check_weights(raw, universe, d)
    return out


# --------------------------------------------------------------------------
# Hard limits on a target
# --------------------------------------------------------------------------

def limit_targets(w: dict[str, float], px: dict[str, float], avg_vol: dict[str, float], params: dict,
                  universe: set[str], benchmark: bool = False) -> tuple[dict[str, float], list[str]]:
    """The live hard limits applied to one decision's weights. Returns the
    allowed weights and what was changed."""
    hl = lconfig.get().hard_limits
    notes = []
    cap = min(float(params.get("position_max_pct", hl.max_position_pct_of_strategy)),
              hl.max_position_pct_of_strategy)
    out = {}
    for t, x in w.items():
        if t not in universe:
            notes.append(f"{t}: not allowed")
            continue
        p, v = px.get(t), avg_vol.get(t)
        if benchmark:
            out[t] = x                     # an index holding: only the universe and no-leverage rules
            continue
        if p is None or not math.isfinite(p) or p < hl.min_price:
            notes.append(f"{t}: price {p} < {hl.min_price}")
            continue
        if v is None or not math.isfinite(v) or v < hl.min_avg_daily_volume:
            notes.append(f"{t}: 20-day volume {v} < {hl.min_avg_daily_volume:.0f}")
            continue
        if abs(x) > cap + 1e-12:
            notes.append(f"{t}: {x:+.3f} capped at {cap:.3f}")
            x = math.copysign(cap, x)
        out[t] = x
    short = sum(-x for x in out.values() if x < 0)
    if short > hl.max_short_exposure_pct + 1e-12:
        k = hl.max_short_exposure_pct / short
        notes.append(f"shorts {short:.3f} scaled to {hl.max_short_exposure_pct:.3f}")
        out = {t: x * k if x < 0 else x for t, x in out.items()}
    gross = sum(abs(x) for x in out.values())
    if gross > hl.max_gross_exposure + 1e-12:
        k = hl.max_gross_exposure / gross
        notes.append(f"gross {gross:.3f} scaled to {hl.max_gross_exposure:.3f}")
        out = {t: x * k for t, x in out.items()}
    return out, notes


# --------------------------------------------------------------------------
# The simulation
# --------------------------------------------------------------------------

@dataclass
class _Episode:
    """One round trip: from flat to flat (or a flip) in one ticker."""
    symbol: str
    side: str
    entry_day: str
    entry_state: str | None
    shares: float = 0.0          # signed, current
    cost: float = 0.0            # total entry value bought/shorted (for pnl %)
    entry_value_shares: float = 0.0
    pnl: float = 0.0             # realized dollars, after costs

    def entry_price(self) -> float:
        return self.cost / self.entry_value_shares if self.entry_value_shares else 0.0


def run(spec: StrategySpec, frame: pd.DataFrame, start: str, end: str, labels: dict[str, str | None] | None = None,
        options: Options | None = None) -> RunResult:
    """Backtest spec on frame (bars must include enough warm-up before start)
    for executions in [start, end]."""
    cfg = labconfig.get()
    opt = options or Options()
    rebalance = opt.rebalance or cfg.engine.rebalance
    capital = opt.capital if opt.capital is not None else cfg.engine.starting_cash
    bps = (opt.bps_per_side if opt.bps_per_side is not None else cfg.costs.bps_per_side) / 1e4
    hl = lconfig.get().hard_limits
    labels = labels or {}
    params = {**lconfig.default_params(), **(spec.params or {})}
    stop_pct = float(params["stop_loss_pct"])
    from .. import universe as uni
    tradable = set(opt.universe) if opt.universe is not None else set(uni.SECTOR_OF)
    frame = frame.loc[:end]
    tickers = [t for t in frame["close"].columns]
    tradable &= set(tickers)

    sched = schedule(frame.index, start, end, rebalance, cfg.engine.min_history_days)
    if not sched:
        raise ValueError(f"no rebalance dates between {start} and {end} (need {cfg.engine.min_history_days} "
                         f"bars of history first)")
    targets = signals(spec, frame, [dec for dec, _ in sched], tradable)

    idx = {t: i for i, t in enumerate(tickers)}
    dates = [d.strftime("%Y-%m-%d") for d in frame.index]
    O, H, L = (frame[f].to_numpy(float) for f in ("open", "high", "low"))
    C_raw = frame["close"].to_numpy(float)
    C = frame["close"].ffill().to_numpy(float)
    vol = frame["volume"]
    avg_vol = vol.rolling(20, min_periods=20).mean().to_numpy(float)
    dollar_vol = (frame["close"] * vol).rolling(20, min_periods=20).mean().to_numpy(float)
    row = {d: i for i, d in enumerate(dates)}
    by_exec = {ex: dec for dec, ex in sched}

    cash = capital
    shares: dict[str, float] = {}
    stops: dict[str, float] = {}
    blocked: set[str] = set()
    episodes: dict[str, _Episode] = {}
    done: list[dict] = []
    notes: list[str] = []
    fills = 0
    borrow_paid: dict[str, pd.Timestamp] = {}
    equity: list[float] = []
    first = row[sched[0][1]]
    last = row[dates[-1]] if dates[-1] <= end else max(i for d, i in row.items() if d <= end)

    def close_episode(t: str, day: str, price: float, reason: str) -> None:
        ep = episodes.pop(t)
        done.append({"symbol": t, "side": ep.side, "entry_day": ep.entry_day, "entry_price": ep.entry_price(),
                     "exit_day": day, "exit_price": price, "shares": ep.entry_value_shares, "pnl": ep.pnl,
                     "pnl_pct": ep.pnl / ep.cost * 100 if ep.cost else 0.0, "exit_reason": reason,
                     "entry_state": ep.entry_state})

    def trade(t: str, delta: float, ref: float, day: str, reason: str, state: str | None) -> None:
        """Fill `delta` shares of t at ref +/- costs; book the round trip."""
        nonlocal cash, fills
        fill = ref * (1 + bps) if delta > 0 else ref * (1 - bps)
        fee = league_costs.commission(delta, fill)
        old = shares.get(t, 0.0)
        new = old + delta
        cash -= delta * fill + fee
        fills += 1
        # Split the order into the part that closes the position and the part
        # that opens one (both only on a flip from long to short or back).
        if old != 0 and (delta > 0) != (old > 0):
            closing = min(abs(delta), abs(old))
        else:
            closing = 0.0
        opening = abs(delta) - closing
        if closing:
            ep = episodes[t]
            sign = 1 if old > 0 else -1
            ep.pnl += sign * closing * (fill - ep.entry_price()) - fee * closing / abs(delta)
            if closing >= abs(old) - 1e-9:
                close_episode(t, day, fill, reason)
        if opening > 1e-9:
            if t not in episodes:
                episodes[t] = _Episode(t, "long" if new > 0 else "short", day, state)
            ep = episodes[t]
            ep.cost += opening * fill
            ep.entry_value_shares += opening
            ep.pnl -= fee * opening / abs(delta)
        if abs(new) < 1e-9:
            shares.pop(t, None)
            stops.pop(t, None)
            borrow_paid.pop(t, None)
        else:
            shares[t] = new
            if new < 0 and old >= 0:
                borrow_paid[t] = pd.Timestamp(day) - pd.Timedelta(days=1)

    for i in range(first, last + 1):
        day = dates[i]
        # 1) Rebalance at the open.
        if day in by_exec:
            dec = by_exec[day]
            j = row[dec]
            state = labels.get(dec)
            px_dec = {t: C_raw[j, idx[t]] for t in targets[dec]}
            av = {t: avg_vol[j, idx[t]] for t in targets[dec]}
            w, lim_notes = limit_targets(targets[dec], px_dec, av, params, tradable, opt.benchmark)
            if lim_notes:
                notes.append(f"{dec}: " + "; ".join(lim_notes[:6]) + (" ..." if len(lim_notes) > 6 else ""))
            blocked.clear()
            eq_open = cash + sum(sh * (O[i, idx[t]] if math.isfinite(O[i, idx[t]]) else C[i - 1, idx[t]])
                                 for t, sh in shares.items())
            orders = []
            for t in sorted(set(shares) | set(w)):
                op = O[i, idx[t]]
                if not math.isfinite(op) or op <= 0:
                    continue                      # no open price: can't trade it today
                tgt = w.get(t, 0.0) * eq_open / op
                if dollar_vol[j, idx[t]] < cfg.costs.fractional_min_dollar_volume:
                    tgt = math.trunc(tgt)         # illiquid: whole shares only
                delta = tgt - shares.get(t, 0.0)
                if not league_costs.is_too_small(delta, op):
                    orders.append((t, delta, op))
            if rebalance == "daily" and len(orders) > hl.max_trades_per_strategy_per_day:
                orders.sort(key=lambda o: -abs(o[1] * o[2]))
                notes.append(f"{day}: trade cap kept {hl.max_trades_per_strategy_per_day} of {len(orders)} orders")
                orders = orders[:hl.max_trades_per_strategy_per_day]
            sells = [o for o in orders if o[1] < 0]
            buys = [o for o in orders if o[1] > 0]
            for t, delta, op in sells:
                trade(t, delta, op, day, "rebalance", state)
            # No leverage: buys (incl. covers) are scaled to the cash there is.
            # Short proceeds are collateral, not buying power.
            # (A cover spends cash but also cancels the same short liability.)
            short_value = sum(-sh * O[i, idx[t]] for t, sh in shares.items() if sh < 0 and math.isfinite(O[i, idx[t]]))
            free = cash - short_value
            need = sum(d * op * (1 + bps) + league_costs.commission(d, op * (1 + bps))
                       for t, d, op in buys if shares.get(t, 0.0) >= 0)
            k = 1.0
            if need > 0 and free < need:
                k = max(0.0, free / need)
                notes.append(f"{day}: buys scaled by {k:.4f} to stay unleveraged")
            for t, delta, op in buys:
                d2 = delta * k if shares.get(t, 0.0) >= 0 else delta
                if not league_costs.is_too_small(d2, op):
                    trade(t, d2, op, day, "rebalance", state)
            # A stop on every position, from the decision close (as live).
            for t, sh in shares.items():
                base = C[j, idx[t]]
                op = O[i, idx[t]]
                if sh > 0:
                    stops[t] = base * (1 - stop_pct)
                    if math.isfinite(op) and op <= stops[t]:
                        stops[t] = op * (1 - stop_pct)      # opened through it: stop from the entry
                else:
                    stops[t] = base * (1 + stop_pct)
                    if math.isfinite(op) and op >= stops[t]:
                        stops[t] = op * (1 + stop_pct)
        # 2) Stops during the day.
        if not opt.benchmark:
            for t in sorted(shares):
                sh, s = shares[t], stops.get(t)
                op, lo, hi = O[i, idx[t]], L[i, idx[t]], H[i, idx[t]]
                if s is None or not math.isfinite(lo):
                    continue
                exec_day = day in by_exec
                if sh > 0 and lo <= s:
                    px = s if exec_day or not math.isfinite(op) or op > s else op
                    trade(t, -sh, px, day, "stop", None)
                    blocked.add(t)
                elif sh < 0 and hi >= s:
                    px = s if exec_day or not math.isfinite(op) or op < s else op
                    trade(t, -sh, px, day, "stop", None)
                    blocked.add(t)
        # 3) The close: borrow fees, mark.
        ts = pd.Timestamp(day)
        for t, sh in shares.items():
            if sh < 0:
                days_owed = (ts - borrow_paid[t]).days
                fee = league_costs.borrow_fee(-sh * C[i, idx[t]], days_owed)
                cash -= fee
                if t in episodes:
                    episodes[t].pnl -= fee
                borrow_paid[t] = ts
        equity.append(cash + sum(sh * C[i, idx[t]] for t, sh in shares.items()))

    # Positions still open at the end are closed at the last close (no costs)
    # so every trade is counted.
    end_day = dates[last]
    for t in sorted(episodes):
        ep = episodes[t]
        px = C[last, idx[t]]
        sign = 1 if ep.side == "long" else -1
        ep.pnl += sign * abs(shares.get(t, 0.0)) * (px - ep.entry_price())
        close_episode(t, end_day, px, "end")

    eq = pd.Series(equity, index=pd.to_datetime(dates[first:last + 1]))
    spy = None
    if "SPY" in idx:
        so, sc = O[first, idx["SPY"]], C[last, idx["SPY"]]
        spy = (sc / so - 1) if math.isfinite(so) and so > 0 else None
    m = metrics(eq, done, fills, capital, [ex for _, ex in sched], labels, dates, spy)
    return RunResult(eq, done, fills, sched, notes, m)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

def _period(values: list[float]) -> tuple[float, float]:
    """(return %, max drawdown %) of a value path."""
    return (values[-1] / values[0] - 1) * 100, max_drawdown_pct(values)


def metrics(eq: pd.Series, trades: list[dict], fills: int, capital: float, exec_days: list[str],
            labels: dict[str, str | None], dates: list[str], spy_return: float | None) -> dict:
    floor = lconfig.get().scoring.drawdown_floor_pct
    values = [capital] + list(eq.values)
    total, mdd = _period(values)
    years = max(len(eq) / 252, 1e-9)
    daily = pd.Series(values).pct_change().dropna()
    sd = float(daily.std()) if len(daily) > 1 else 0.0
    # The league's weekly score: return % / max(max drawdown %, 1%), per
    # rebalance week (open -> next rebalance open, approximated by closes).
    days = [d.strftime("%Y-%m-%d") for d in eq.index]
    cuts = [days.index(d) for d in exec_days if d in days] + [len(days)]
    weekly = []
    for a, b in zip(cuts, cuts[1:]):
        prev = values[a]               # equity before the rebalance day (values is offset by 1)
        path = [prev] + list(eq.values[a:b])
        r, dd = _period(path)
        weekly.append(league_score(r, dd, False, floor))
    closed = trades
    wins = [t for t in closed if t["pnl"] > 0]
    # Per market state: day d's return belongs to the state at the close before it.
    by_state: dict[str, dict] = {}
    pos = {d: i for i, d in enumerate(dates)}
    for k, d in enumerate(days):
        prev_day = dates[pos[d] - 1] if pos[d] > 0 else None
        st = labels.get(prev_day) if prev_day else None
        st = st or "unlabelled"
        r = values[k + 1] / values[k] - 1
        e = by_state.setdefault(st, {"days": 0, "growth": 1.0, "trades": 0, "wins": 0, "trade_pct": 0.0})
        e["days"] += 1
        e["growth"] *= 1 + r
    for t in closed:
        st = t["entry_state"] or "unlabelled"
        e = by_state.setdefault(st, {"days": 0, "growth": 1.0, "trades": 0, "wins": 0, "trade_pct": 0.0})
        e["trades"] += 1
        e["wins"] += t["pnl"] > 0
        e["trade_pct"] += t["pnl_pct"]
    states = {}
    for st, e in sorted(by_state.items()):
        states[st] = {"days": e["days"], "return_pct": round((e["growth"] - 1) * 100, 3),
                      "annual_return_pct": round((e["growth"] ** (252 / e["days"]) - 1) * 100, 3) if e["days"] else None,
                      "trades": e["trades"],
                      "win_rate_pct": round(e["wins"] / e["trades"] * 100, 1) if e["trades"] else None,
                      "avg_trade_pct": round(e["trade_pct"] / e["trades"], 3) if e["trades"] else None}
    spy_pct = None if spy_return is None else spy_return * 100
    return {
        "start": days[0], "end": days[-1], "trading_days": len(days), "rebalances": len(exec_days),
        "total_return_pct": round(total, 3),
        "annual_return_pct": round(((1 + total / 100) ** (1 / years) - 1) * 100, 3) if total > -100 else -100.0,
        "max_drawdown_pct": round(mdd, 3),
        "sharpe": round(float(daily.mean() / sd * np.sqrt(252)), 3) if sd > 0 else 0.0,
        "annual_vol_pct": round(sd * np.sqrt(252) * 100, 3),
        "risk_adjusted_score": round(float(np.mean(weekly)), 4) if weekly else 0.0,
        "period_score": round(league_score(total, mdd, False, floor), 4),
        "trades": len(closed), "fills": fills,
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "spy_return_pct": None if spy_pct is None else round(spy_pct, 3),
        "vs_spy_pct": None if spy_pct is None else round(total - spy_pct, 3),
        "by_state": states,
        "final_equity": round(values[-1], 2),
        "data_note": labconfig.get().survivorship_note,
        "costs": {"bps_per_side": labconfig.get().costs.bps_per_side,
                  "commission": "league model (arena/costs.py)", "borrow_fee_annual": SETTINGS.costs.borrow_fee_annual},
    }
