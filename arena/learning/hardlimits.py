"""Anti-gambling hard limits (config/learning.yaml `hard_limits:`).

Enforced in code at two layers, whatever the agent says:

  1. Decision time: validate_slots() rejects a plan (the agent gets the
     errors back and can fix them, like every other validation error).
  2. Order time: check_order() runs before EVERY agent-initiated fill
     (weekly rebalance, tilts, emergency rewrites, risk exits), so nothing
     reaches the ledger, and through the mirror Alpaca, that breaks a limit.
     A violating order is skipped and logged in rejected_changes.

audit_trades() re-checks the fills of a week afterwards; any violation that
got through anyway marks the week broke_hard_limit (score 0).

The limits apply from scoring.start_week (the next 4-week period), and only
with LEARNING_ENABLED=1; before that the original limits in arena/config.py
are the only ones.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import universe
from ..ledger import SlotTarget
from ..prices import price_on_or_before
from . import config as lconfig

EPS = 1e-6
# Exits the engine forces (or the agent's own stops): never blocked, never counted.
FORCED_REASONS = ("stop_loss", "take_profit", "forced_cover")
# Whole-portfolio moves: the scheduled weekly rebalance and capital tilts
# between strategies (see trade_cap_includes_weekly_rebalance).
REBALANCE_REASONS = ("rebalance", "tilt")
_CRYPTO = re.compile(r"(/|USDT?$|^BTC|^ETH)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_week(conn: sqlite3.Connection) -> int | None:
    """The league week whose positions are live (or, before any trade, the next one)."""
    row = conn.execute("SELECT MAX(week_no) FROM weeks WHERE status IN ('executed', 'scored')").fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute("SELECT MAX(week_no) FROM weeks").fetchone()
    return row[0] if row else None


def active(conn: sqlite3.Connection, week_no: int | None = None) -> bool:
    """Do the new hard limits apply to week_no (default: the current week)?"""
    if not lconfig.enabled():
        return False
    week_no = week_no if week_no is not None else current_week(conn)
    return week_no is not None and week_no >= lconfig.get().scoring.start_week


def instrument_type(ticker: str) -> str | None:
    """'us_etfs' | 'us_stocks' | None (not allowed: options, crypto, leveraged
    or inverse ETFs, anything outside the league's universe)."""
    t = ticker.upper().strip()
    if not t or _CRYPTO.search(t) or len(t) > 6 or not re.fullmatch(r"[A-Z.]+", t):
        return None             # option symbols, crypto pairs, junk
    if t in universe.ETFS:
        return "us_etfs"        # a fixed list of plain (1x, long-only) ETFs
    if universe.is_tradable(t):
        return "us_stocks"
    return None


def avg_daily_volume(conn: sqlite3.Connection, ticker: str, d: str, days: int = 20) -> float | None:
    rows = conn.execute("SELECT volume FROM prices WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT ?",
                        (ticker, d, days)).fetchall()
    if len(rows) < min(days, 5):
        return None
    return sum(r[0] for r in rows) / len(rows)


def instrument_errors(conn: sqlite3.Connection, ticker: str, d: str, price: float | None = None) -> list[str]:
    """Allowed instrument, price >= min_price, 20-day volume >= min_avg_daily_volume."""
    hl = lconfig.get().hard_limits
    kind = instrument_type(ticker)
    if kind is None or kind not in hl.allowed_instruments:
        return [f"{ticker}: not an allowed instrument (only {', '.join(hl.allowed_instruments)} from the "
                f"league's universe; no options, leveraged/inverse ETFs or crypto)."]
    errs = []
    if price is None:
        p = price_on_or_before(conn, ticker, d)
        price = p[1] if p else None
    if price is not None and price < hl.min_price:
        errs.append(f"{ticker}: price {price:.2f} is below the {hl.min_price:.2f} minimum (no penny stocks).")
    adv = avg_daily_volume(conn, ticker, d)
    if adv is not None and adv < hl.min_avg_daily_volume:
        errs.append(f"{ticker}: 20-day average volume {adv:,.0f} is below the {hl.min_avg_daily_volume:,.0f} "
                    f"minimum (liquid names only).")
    return errs


# --------------------------------------------------------------------------
# 1. Decision time
# --------------------------------------------------------------------------

def validate_slots(conn: sqlite3.Connection, targets: list[SlotTarget], d: str,
                   held: dict[tuple[int, str], float] | None = None) -> list[str]:
    """Every hard limit that can be checked on a plan. `held`: current
    {(slot, ticker): weight}, so reducing an existing position is never
    blocked by instrument/price/volume rules."""
    hl = lconfig.get().hard_limits
    held = held or {}
    errors: list[str] = []
    for t in targets:
        gross = sum(abs(w) for w in t.weights.values())
        short = -sum(w for w in t.weights.values() if w < 0)
        if gross > hl.max_gross_exposure + EPS:
            errors.append(f"Slot {t.slot}: gross exposure {gross:.0%} of the strategy is above "
                          f"{hl.max_gross_exposure:.0%} (no leverage).")
        if short > hl.max_short_exposure_pct + EPS:
            errors.append(f"Slot {t.slot}: shorts are {short:.0%} of the strategy; the cap is "
                          f"{hl.max_short_exposure_pct:.0%}.")
        for ticker, w in sorted(t.weights.items()):
            if not w:
                continue
            if abs(w) > hl.max_position_pct_of_strategy + EPS:
                errors.append(f"Slot {t.slot} {ticker}: {abs(w):.0%} of the strategy; no single position may "
                              f"exceed {hl.max_position_pct_of_strategy:.0%}.")
            was = held.get((t.slot, ticker), 0.0)
            increasing = abs(w) > abs(was) + EPS or (was and w * was < 0)
            if increasing:
                errors += [f"Slot {t.slot} {e}" for e in instrument_errors(conn, ticker, d)]
            if hl.every_position_needs_stop and not (t.risk_orders.get(ticker) or {}).get("stop_loss"):
                errors.append(f"Slot {t.slot} {ticker}: no stop_loss. Every position needs one "
                              f"(risk_orders: {{\"{ticker}\": {{\"stop_loss\": price}}}}).")
    return errors


def attach_default_stops(conn: sqlite3.Connection, t: SlotTarget, d: str, stop_loss_pct: float) -> list[str]:
    """Code slots choose their tickers at run time, so the agent can't name a
    stop for each in advance: the engine attaches one at the strategy's
    stop_loss_pct from the last close. Returns a note per stop attached."""
    notes = []
    for ticker, w in t.weights.items():
        if not w or (t.risk_orders.get(ticker) or {}).get("stop_loss"):
            continue
        p = price_on_or_before(conn, ticker, d)
        if p is None:
            continue
        level = p[1] * (1 - stop_loss_pct) if w > 0 else p[1] * (1 + stop_loss_pct)
        t.risk_orders = {**t.risk_orders, ticker: {**(t.risk_orders.get(ticker) or {}), "stop_loss": round(level, 4)}}
        notes.append(f"slot {t.slot} {ticker}: engine stop at {level:.2f} ({stop_loss_pct:.0%})")
    return notes


# --------------------------------------------------------------------------
# 2. Order time
# --------------------------------------------------------------------------

@dataclass
class Order:
    agent_id: str
    slot: int
    ticker: str
    delta: float                 # signed shares
    price: float                 # reference price for the fill
    d: str                       # trading day
    reason: str                  # rebalance | tilt | emergency | risk_exit | ...
    held_shares: float           # position in this slot before the order
    slot_equity: float           # the strategy's equity the position is measured against
    has_stop: bool               # a stop_loss will be attached to the resulting position


def trades_today(conn: sqlite3.Connection, agent_id: str, slot: int, d: str) -> int:
    """Fills that count towards the daily cap for this strategy."""
    hl = lconfig.get().hard_limits
    excluded = list(FORCED_REASONS) + ([] if hl.trade_cap_includes_weekly_rebalance else list(REBALANCE_REASONS))
    return conn.execute(
        f"SELECT COUNT(*) FROM trades WHERE agent_id=? AND slot=? AND date=? "
        f"AND reason NOT IN ({','.join('?' * len(excluded))}) AND COALESCE(note, '') NOT LIKE '[reduce-only]%'",
        (agent_id, slot, d, *excluded)).fetchone()[0]


def reduces(o: Order) -> bool:
    """Only shrinks an existing position towards zero (never flips it)."""
    new = o.held_shares + o.delta
    return o.held_shares != 0 and abs(new) < abs(o.held_shares) + EPS and new * o.held_shares >= -EPS


def check_order(conn: sqlite3.Connection, o: Order) -> list[str]:
    """Violations of this order; empty = it may be filled."""
    hl = lconfig.get().hard_limits
    if o.reason in FORCED_REASONS or reduces(o):
        return []               # exits are always allowed: blocking them adds risk
    errs = list(instrument_errors(conn, o.ticker, o.d, o.price))
    new_value = abs((o.held_shares + o.delta) * o.price)
    if o.slot_equity <= 0:
        errs.append(f"{o.ticker}: the strategy has no equity to size the position against.")
    elif new_value > hl.max_position_pct_of_strategy * o.slot_equity * (1 + 0.02) + 0.01:
        # 2% slack: the plan was sized on the same prices, so only rounding gets here.
        errs.append(f"{o.ticker}: position would be {new_value / o.slot_equity:.1%} of the strategy "
                    f"(max {hl.max_position_pct_of_strategy:.0%}).")
    if hl.every_position_needs_stop and not o.has_stop:
        errs.append(f"{o.ticker}: no stop-loss attached to the position.")
    counts = o.reason not in REBALANCE_REASONS or hl.trade_cap_includes_weekly_rebalance
    if counts and trades_today(conn, o.agent_id, o.slot, o.d) >= hl.max_trades_per_strategy_per_day:
        errs.append(f"slot {o.slot}: already {hl.max_trades_per_strategy_per_day} trades today "
                    f"(the daily cap per strategy).")
    return errs


def reject(conn: sqlite3.Connection, agent_id: str, source: str, slot: int | None, kind: str,
           detail: dict, reason: str, review_run_id: int | None = None) -> None:
    """Log a refused change (shown on the dashboard's 'limits enforced' log)."""
    conn.execute("INSERT INTO rejected_changes(ts, agent_id, source, slot, kind, detail, reason, review_run_id) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (_now(), agent_id, source, slot, kind, json.dumps(detail, default=str), reason[:1000], review_run_id))


# --------------------------------------------------------------------------
# 3. After the fact
# --------------------------------------------------------------------------

def audit_trades(conn: sqlite3.Connection, agent_id: str, after_seq: int, upto_seq: int) -> list[str]:
    """Re-check the fills in (after_seq, upto_seq]: instrument, penny stock,
    liquidity (opening/increasing fills only) and the daily trade cap."""
    hl = lconfig.get().hard_limits
    rows = conn.execute("SELECT * FROM trades WHERE agent_id=? AND seq>? AND seq<=? ORDER BY seq",
                        (agent_id, after_seq, upto_seq)).fetchall()
    held: dict[tuple[int, str], float] = {}
    for r in conn.execute("SELECT slot, ticker, shares FROM trades WHERE agent_id=? AND seq<=? ORDER BY seq",
                          (agent_id, after_seq)):
        held[(r["slot"], r["ticker"])] = held.get((r["slot"], r["ticker"]), 0.0) + r["shares"]
    violations: list[str] = []
    per_day: dict[tuple[int, str], int] = {}
    excluded = set(FORCED_REASONS) | (set() if hl.trade_cap_includes_weekly_rebalance else set(REBALANCE_REASONS))
    for r in rows:
        key = (r["slot"], r["ticker"])
        before = held.get(key, 0.0)
        after = before + r["shares"]
        held[key] = after
        if r["reason"] in FORCED_REASONS:
            continue
        increasing = abs(after) > abs(before) + 1e-9 or before * after < 0
        if increasing:
            for e in instrument_errors(conn, r["ticker"], r["date"], r["ref_price"]):
                violations.append(f"{r['date']} slot {r['slot']} {e}")
        if r["reason"] not in excluded and not (r["note"] or "").startswith("[reduce-only]"):
            k = (r["slot"], r["date"])
            per_day[k] = per_day.get(k, 0) + 1
            if per_day[k] == hl.max_trades_per_strategy_per_day + 1:
                violations.append(f"{r['date']} slot {r['slot']}: more than "
                                  f"{hl.max_trades_per_strategy_per_day} trades in one day.")
    return violations
