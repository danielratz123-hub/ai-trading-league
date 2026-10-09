"""Hard limits and daily risk processing.

Agents propose, this module checks, the ledger executes. Nothing an agent
sends is trusted: every number is re-checked here, and errors are written so
an LLM can read them and fix its decision.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field

from . import costs, ledger
from .config import SETTINGS, Limits
from .db import log_event, next_seq, transaction
from .ledger import SlotTarget
from .prices import get_price
from .trading_calendar import calendar_days_between, next_trading_day
from .universe import is_tradable

EPS = 1e-6  # tolerance so 0.25 written as 0.2500000001 still passes


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]
    exposures: dict = field(default_factory=dict)

    def message(self) -> str:
        """Readable summary to send back to an agent."""
        e = self.exposures
        lines = ["Your decision was REJECTED by the risk engine:"]
        lines += [f"  - {err}" for err in self.errors]
        if e:
            lines.append(
                f"Computed exposures (as % of equity): gross {e['gross']:.1%}, "
                f"long {e['long']:.1%}, short {e['short']:.1%}.")
        return "\n".join(lines)


def combined_weights(slots: list[SlotTarget]) -> dict[str, float]:
    """Net weight per ticker as a fraction of total equity, across all slots."""
    net: dict[str, float] = {}
    for s in slots:
        for t, w in s.weights.items():
            net[t] = net.get(t, 0.0) + s.allocation * w
    return net


def validate_portfolio(slots: list[SlotTarget], last_prices: dict[str, float] | None = None,
                       limits: Limits = SETTINGS.limits) -> ValidationResult:
    """Check a proposed portfolio against every hard limit.

    last_prices (latest close per ticker) is only used to sanity-check stop
    and take-profit levels; pass None to skip that check.
    """
    errors: list[str] = []

    # Structure
    nums = [s.slot for s in slots]
    if len(slots) > limits.max_slots:
        errors.append(f"{len(slots)} slots used; the maximum is {limits.max_slots}.")
    if len(nums) != len(set(nums)):
        errors.append(f"Duplicate slot numbers: {sorted(nums)}.")
    for n in nums:
        if not (1 <= n <= limits.max_slots):
            errors.append(f"Slot number {n} is invalid; use 1..{limits.max_slots}.")

    total_alloc = 0.0
    for s in slots:
        if not _finite(s.allocation) or s.allocation < 0:
            errors.append(f"Slot {s.slot}: allocation {s.allocation!r} must be a number >= 0.")
            continue
        total_alloc += s.allocation
        if s.type not in ("manual", "code"):
            errors.append(f"Slot {s.slot}: type must be 'manual' or 'code', got {s.type!r}.")
        bad = [t for t, w in s.weights.items() if not _finite(w)]
        if bad:
            errors.append(f"Slot {s.slot}: weights for {bad} are not valid numbers.")
            continue
        unknown = sorted(t for t in s.weights if not is_tradable(t))
        if unknown:
            errors.append(f"Slot {s.slot}: {unknown} not in the tradable universe "
                          f"(S&P 500 + major ETFs).")
        slot_gross = sum(abs(w) for w in s.weights.values())
        if slot_gross > 1 + EPS:
            errors.append(f"Slot {s.slot}: sum of |weights| is {slot_gross:.4f}; it must be <= 1 "
                          f"(weights are fractions of the slot's money).")
    if total_alloc > 1 + EPS:
        errors.append(f"Total allocation across slots is {total_alloc:.4f}; it must be <= 1.")

    if errors:  # numbers below would be meaningless
        return ValidationResult(False, errors)

    # Portfolio-level limits on the net exposure per ticker.
    net = combined_weights(slots)
    long_total = sum(w for w in net.values() if w > 0)
    short_total = -sum(w for w in net.values() if w < 0)
    gross = long_total + short_total
    exposures = {"gross": gross, "long": long_total, "short": short_total,
                 "per_ticker": {t: round(w, 6) for t, w in sorted(net.items()) if abs(w) > EPS}}

    if gross > limits.max_gross + EPS:
        errors.append(f"Gross exposure {gross:.1%} exceeds the {limits.max_gross:.0%} limit (no leverage).")
    if short_total > limits.max_short_total + EPS:
        errors.append(f"Total short exposure {short_total:.1%} exceeds the {limits.max_short_total:.0%} limit.")
    for t, w in sorted(net.items()):
        if w > limits.max_long_per_ticker + EPS:
            errors.append(f"{t}: net long {w:.1%} of equity exceeds the "
                          f"{limits.max_long_per_ticker:.0%} per-ticker limit (summed across slots).")
        if -w > limits.max_short_per_ticker + EPS:
            errors.append(f"{t}: net short {-w:.1%} of equity exceeds the "
                          f"{limits.max_short_per_ticker:.0%} per-ticker limit (summed across slots).")

    # Risk orders: must refer to a held position and make sense directionally,
    # otherwise they'd fire immediately at the next close.
    for s in slots:
        for t, o in s.risk_orders.items():
            w = s.weights.get(t, 0.0)
            sl, tp = o.get("stop_loss"), o.get("take_profit")
            for label, v in (("stop_loss", sl), ("take_profit", tp)):
                if v is not None and (not _finite(v) or v <= 0):
                    errors.append(f"Slot {s.slot} {t}: {label} {v!r} must be a positive price.")
            if w == 0:
                errors.append(f"Slot {s.slot} {t}: risk order given but the slot has no position in {t}.")
                continue
            px = (last_prices or {}).get(t)
            if px is None:
                continue
            long = w > 0
            if sl is not None and _finite(sl) and ((long and sl >= px) or (not long and sl <= px)):
                errors.append(f"Slot {s.slot} {t}: stop_loss {sl} is on the wrong side of the last "
                              f"close {px:.2f} for a {'long' if long else 'short'} position.")
            if tp is not None and _finite(tp) and ((long and tp <= px) or (not long and tp >= px)):
                errors.append(f"Slot {s.slot} {t}: take_profit {tp} is on the wrong side of the last "
                              f"close {px:.2f} for a {'long' if long else 'short'} position.")

    return ValidationResult(not errors, errors, exposures)


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


# --------------------------------------------------------------------------
# Daily processing
# --------------------------------------------------------------------------

@dataclass
class DailyReport:
    executed: list[ledger.Trade] = field(default_factory=list)
    borrow_fees: float = 0.0
    new_orders: list[str] = field(default_factory=list)
    skipped: bool = False


def execute_pending(conn: sqlite3.Connection, agent_id: str, d: str,
                    kinds: tuple[str, ...] = ("stop_loss", "take_profit", "forced_cover")) -> list[ledger.Trade]:
    """Execute risk-generated orders due at date d's open."""
    trades = []
    rows = conn.execute(
        f"SELECT * FROM pending_orders WHERE agent_id=? AND status='pending' AND exec_date<=? "
        f"AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id",
        (agent_id, d, *kinds)).fetchall()
    for o in rows:
        held = ledger.position_shares(conn, agent_id, o["slot"], o["ticker"])
        price = get_price(conn, o["ticker"], d, "open")
        if o["kind"] == "forced_cover":
            # Never cover more than is still short (agent may have reduced it).
            delta = min(o["shares"], -held) if held < 0 else 0.0
        else:
            delta = -held  # stops and take-profits close the whole position
        status, note = "done", None
        if price is None:
            status, note = "pending", f"no open price on {d}; retry next day"
        elif abs(delta) < ledger.DUST:
            status, note = "skipped", "position already closed"
        else:
            why = {"stop_loss": "Stop-loss", "take_profit": "Take-profit",
                   "forced_cover": "Forced cover (engine)"}.get(o["kind"], o["kind"])
            tr = ledger.execute_trade(conn, agent_id, o["slot"], o["ticker"], delta, d, price, o["kind"],
                                      f"{why}: {o['note']}" if o["note"] else why)
            if tr:
                trades.append(tr)
            else:
                status, note = "skipped", "below minimum trade size"
        if status == "pending":
            conn.execute("UPDATE pending_orders SET note=? WHERE id=?", (note, o["id"]))
        else:
            conn.execute("UPDATE pending_orders SET status=?, note=? WHERE id=?", (status, note, o["id"]))
    return trades


def charge_borrow_fees(conn: sqlite3.Connection, agent_id: str, d: str) -> float:
    """Charge borrow on every short from its paid-through date up to d."""
    total = 0.0
    for p in ledger.positions(conn, agent_id):
        if p["shares"] >= 0 or p["borrow_paid_through"] is None:
            continue
        days = calendar_days_between(p["borrow_paid_through"], d)
        if days <= 0:
            continue
        price, _ = ledger.mark_price(conn, p["ticker"], d, "close")
        short_value = -p["shares"] * price
        fee = costs.borrow_fee(short_value, days)
        conn.execute("UPDATE slots SET cash = cash - ? WHERE agent_id=? AND slot=?",
                     (fee, agent_id, p["slot"]))
        conn.execute("UPDATE positions SET borrow_paid_through=? WHERE agent_id=? AND slot=? AND ticker=?",
                     (d, agent_id, p["slot"], p["ticker"]))
        conn.execute(
            "INSERT INTO borrow_fees(seq, agent_id, slot, date, ticker, short_value, days, amount) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (next_seq(conn), agent_id, p["slot"], d, p["ticker"], short_value, days, fee))
        total += fee
    return total


def _add_pending(conn, agent_id, created, exec_date, kind, slot, ticker, shares, note) -> str:
    dup = conn.execute(
        "SELECT 1 FROM pending_orders WHERE agent_id=? AND status='pending' AND kind=? "
        "AND slot=? AND ticker=?", (agent_id, kind, slot, ticker)).fetchone()
    if dup:
        return f"{kind} {ticker} slot {slot}: already pending"
    conn.execute(
        "INSERT INTO pending_orders(agent_id, created_date, exec_date, kind, slot, ticker, shares, note) "
        "VALUES (?,?,?,?,?,?,?,?)", (agent_id, created, exec_date, kind, slot, ticker, shares, note))
    return f"{kind} {ticker} slot {slot}: {note}"


def check_risk_orders(conn: sqlite3.Connection, agent_id: str, d: str, exec_date: str) -> list[str]:
    """Stops / take-profits are checked on the close and filled at the next open."""
    out = []
    rows = conn.execute(
        "SELECT r.*, p.shares FROM risk_orders r JOIN positions p "
        "ON p.agent_id=r.agent_id AND p.slot=r.slot AND p.ticker=r.ticker WHERE r.agent_id=?",
        (agent_id,)).fetchall()
    for r in rows:
        close = get_price(conn, r["ticker"], d, "close")
        if close is None:
            continue
        long = r["shares"] > 0
        sl, tp = r["stop_loss"], r["take_profit"]
        kind = None
        if sl is not None and ((long and close <= sl) or (not long and close >= sl)):
            kind, level = "stop_loss", sl
        elif tp is not None and ((long and close >= tp) or (not long and close <= tp)):
            kind, level = "take_profit", tp
        if kind:
            out.append(_add_pending(conn, agent_id, d, exec_date, kind, r["slot"], r["ticker"], None,
                                    f"close {close:.2f} crossed {kind} {level:.2f}"))
            # One-shot: remove it so it doesn't trigger again tomorrow.
            conn.execute("DELETE FROM risk_orders WHERE agent_id=? AND slot=? AND ticker=?",
                         (agent_id, r["slot"], r["ticker"]))
    return out


def plan_forced_covers(val: ledger.Valuation, limits: Limits = SETTINGS.limits) -> dict[str, float]:
    """Dollars of short to cover per ticker, or {} if no trigger fired.

    Triggers: any single short > 20% of equity, or gross > 120%.
    Covers bring things back to the normal hard limits (15% per ticker,
    100% gross), largest shorts first.
    """
    eq = val.equity
    shorts = {t: -v for t, v in val.ticker_value.items() if v < 0}
    if not shorts:
        return {}
    if eq <= 0:
        return dict(shorts)  # account wiped out: close every short
    covers: dict[str, float] = {}
    for t, s in shorts.items():
        if s > limits.forced_cover_single_short * eq:
            covers[t] = s - limits.max_short_per_ticker * eq
    if val.gross > limits.forced_cover_gross * eq:
        excess = val.gross - sum(covers.values()) - limits.max_gross * eq
        for t in sorted(shorts, key=lambda t: -(shorts[t] - covers.get(t, 0.0))):
            if excess <= 0:
                break
            remaining = shorts[t] - covers.get(t, 0.0)
            extra = min(remaining, excess)
            covers[t] = covers.get(t, 0.0) + extra
            excess -= extra
    return {t: c for t, c in covers.items() if c > 0}


def check_forced_covers(conn: sqlite3.Connection, agent_id: str, d: str, exec_date: str) -> list[str]:
    val = ledger.value_agent(conn, agent_id, d, "close")
    out = []
    for ticker, dollars in sorted(plan_forced_covers(val).items()):
        price = val.prices[ticker]
        # Spread the cover across the slots holding that short, pro rata.
        rows = conn.execute("SELECT slot, shares FROM positions WHERE agent_id=? AND ticker=? AND shares<0",
                            (agent_id, ticker)).fetchall()
        total_short = -sum(r["shares"] for r in rows)
        cover_shares = min(dollars / price, total_short)
        for r in rows:
            part = cover_shares * (-r["shares"]) / total_short
            pct = -val.ticker_value[ticker] / val.equity if val.equity > 0 else float("inf")
            out.append(_add_pending(conn, agent_id, d, exec_date, "forced_cover", r["slot"], ticker, part,
                                    f"short {ticker} at {pct:.1%} of equity, gross "
                                    f"{val.gross / val.equity if val.equity > 0 else float('inf'):.1%}"))
    return out


def process_daily(conn: sqlite3.Connection, agent_id: str, d: str) -> DailyReport:
    """Everything that happens once per trading day for one agent:
       1. fill stop / take-profit / forced-cover orders due at today's open
       2. charge borrow fees on shorts
       3. check stops and take-profits on today's close
       4. check forced-cover triggers on today's close
       5. write the 'daily' snapshot
    Idempotent: running it twice for the same day does nothing the 2nd time.
    """
    rep = DailyReport()
    done = conn.execute("SELECT 1 FROM snapshots WHERE agent_id=? AND date=? AND kind='daily'",
                        (agent_id, d)).fetchone()
    if done:
        rep.skipped = True
        return rep
    with transaction(conn):
        nxt = next_trading_day(conn, d)
        # Orders due at today's open fill only in the morning job (run_open).
        # If that didn't run, they wait for the next open instead of filling
        # now at a price from before today's (already known) close.
        conn.execute("UPDATE pending_orders SET exec_date=?, note=COALESCE(note,'') || ' (morning run missed; moved)' "
                     "WHERE agent_id=? AND status='pending' AND kind!='rebalance' AND exec_date<=?",
                     (nxt, agent_id, d))
        rep.borrow_fees = charge_borrow_fees(conn, agent_id, d)
        rep.new_orders = check_risk_orders(conn, agent_id, d, nxt)
        rep.new_orders += check_forced_covers(conn, agent_id, d, nxt)
        ledger.snapshot(conn, agent_id, d, "daily", "close")
        for msg in rep.new_orders:
            log_event(conn, "info", f"daily:{agent_id}", msg)
        if rep.executed:
            log_event(conn, "info", f"daily:{agent_id}",
                      json.dumps([f"{t.reason} {t.ticker} {t.shares:+.4f}@{t.fill_price:.2f}"
                                  for t in rep.executed]))
    return rep
