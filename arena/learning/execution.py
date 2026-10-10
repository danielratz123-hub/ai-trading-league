"""Orders created by the learning layer, all through the hard-limit check.

  * tilts and emergency rewrites (daily review): stored as pending orders,
    filled at the next open by run_open(), unless the weekly rebalance
    trades that morning (it replaces the whole portfolio anyway)
  * risk exits (hourly risk wake-up): filled immediately at the latest
    1-minute bar's price (+ the usual slippage and commission)

Every fill goes through hardlimits.check_order first; a refused order is
skipped and logged in rejected_changes.
"""
from __future__ import annotations

import json
import sqlite3

from .. import ledger
from ..db import log_event
from ..ledger import SlotTarget
from ..trading_calendar import next_trading_day
from . import hardlimits, journal

PENDING_KINDS = ("tilt", "emergency")


# --------------------------------------------------------------------------
# The portfolio as targets
# --------------------------------------------------------------------------

def current_targets(conn: sqlite3.Connection, agent_id: str, val: ledger.Valuation) -> list[SlotTarget]:
    """The agent's portfolio right now, expressed as SlotTargets (weights
    within each slot at val's prices), so a change to one slot can go through
    ledger.rebalance without touching the others."""
    eq = val.equity
    risk_orders: dict[int, dict] = {}
    for r in conn.execute("SELECT * FROM risk_orders WHERE agent_id=?", (agent_id,)):
        risk_orders.setdefault(r["slot"], {})[r["ticker"]] = {
            k: r[k] for k in ("stop_loss", "take_profit") if r[k] is not None}
    pos: dict[int, dict[str, float]] = {}
    for p in ledger.positions(conn, agent_id):
        pos.setdefault(p["slot"], {})[p["ticker"]] = p["shares"] * val.prices[p["ticker"]]
    out = []
    for s in conn.execute("SELECT * FROM slots WHERE agent_id=? AND slot>0 AND active=1 ORDER BY slot", (agent_id,)):
        s_eq = val.slot_equity.get(s["slot"], 0.0)
        if eq <= 0 or s_eq <= 0:
            continue
        weights = {t: v / s_eq for t, v in pos.get(s["slot"], {}).items()}
        out.append(SlotTarget(s["slot"], s_eq / eq, weights, s["name"], s["type"],
                              risk_orders=risk_orders.get(s["slot"], {}), reason="(unchanged)"))
    return out


def order_check(conn: sqlite3.Connection, agent_id: str, d: str, targets: list[SlotTarget], equity: float,
                reason: str, source: str, analysis_date: str | None = None):
    """A callback for ledger.rebalance: returns True to allow (slot, ticker, delta, price).
    analysis_date: the close the decision was based on (stale-analysis check)."""
    from ..prices import price_on_or_before
    by_slot = {t.slot: t for t in targets}
    loss = hardlimits.day_loss(conn, agent_id, d, equity)

    def check(slot: int, ticker: str, delta: float, price: float) -> bool:
        t = by_slot.get(slot)
        ref = price_on_or_before(conn, ticker, analysis_date) if analysis_date else None
        o = hardlimits.Order(agent_id, slot, ticker, delta, price, d, reason,
                             ledger.position_shares(conn, agent_id, slot, ticker),
                             (t.allocation * equity) if t else 0.0,
                             bool(t and (t.risk_orders.get(ticker) or {}).get("stop_loss")),
                             day_loss=loss, analysis_price=ref[1] if ref else None)
        errs = hardlimits.check_order(conn, o)
        if errs:
            hardlimits.reject(conn, agent_id, "order", slot, "order",
                              {"ticker": ticker, "shares": delta, "price": price, "reason": reason, "via": source},
                              "; ".join(errs))
            log_event(conn, "warning", f"hard-limit:{agent_id}", f"order refused: slot {slot} {ticker} "
                      f"{delta:+.4f} @ {price:.2f}: {'; '.join(errs)}")
            return False
        return True
    return check


# --------------------------------------------------------------------------
# Pending tilts / emergency rewrites (filled at the next open)
# --------------------------------------------------------------------------

def queue(conn: sqlite3.Connection, agent_id: str, d: str, kind: str, payload: dict, note: str) -> int:
    assert kind in PENDING_KINDS
    exec_date = next_trading_day(conn, d)
    return conn.execute("INSERT INTO pending_orders(agent_id, created_date, exec_date, kind, payload, note) "
                        "VALUES (?,?,?,?,?,?)", (agent_id, d, exec_date, kind, json.dumps(payload), note[:500])).lastrowid


def run_open(conn: sqlite3.Connection, d: str, weekly_executed: bool) -> dict[str, int]:
    """Fill the tilts / emergency rewrites due at d's open. Returns fills per agent."""
    fills: dict[str, int] = {}
    rows = conn.execute(f"SELECT * FROM pending_orders WHERE status='pending' AND exec_date<=? AND kind IN "
                        f"({','.join('?' * len(PENDING_KINDS))}) ORDER BY id", (d, *PENDING_KINDS)).fetchall()
    from ..league import agent_paused
    for o in rows:
        a = o["agent_id"]
        if agent_paused(conn, a):
            conn.execute("UPDATE pending_orders SET status='skipped', note=note || ' | agent paused (kill switch)' "
                         "WHERE id=?", (o["id"],))
            continue
        if weekly_executed:
            conn.execute("UPDATE pending_orders SET status='skipped', note=note || ' | superseded by the weekly "
                         "rebalance' WHERE id=?", (o["id"],))
            continue
        payload = json.loads(o["payload"])
        val = ledger.value_agent(conn, a, d, "open")
        targets = current_targets(conn, a, val)
        if o["kind"] == "tilt":
            alloc = {int(k): float(v) for k, v in payload["allocations"].items()}
            for t in targets:
                if t.slot in alloc:
                    t.allocation = alloc[t.slot]
            reason = "tilt"
        else:
            slot = int(payload["slot"])
            targets = [t for t in targets if t.slot != slot] + [SlotTarget(
                slot, payload["allocation"], {k: float(v) for k, v in payload["positions"].items()},
                payload["name"], "manual", risk_orders=payload.get("risk_orders") or {},
                reason=payload.get("reason", "emergency rewrite"))]
            reason = "emergency"
        seq0 = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM trades").fetchone()[0]
        res = ledger.rebalance(conn, a, d, targets, reason=reason,
                               order_check=order_check(conn, a, d, targets, val.equity, reason, o["kind"],
                                                       analysis_date=o["created_date"]))
        journal.link_trades(conn, a, d, "daily_review", None, after_seq=seq0)
        conn.execute("UPDATE pending_orders SET status='done', note=COALESCE(note,'') || ? WHERE id=?",
                     (f" | {len(res.trades)} fill(s)" + (f"; skipped {res.skipped}" if res.skipped else ""), o["id"]))
        fills[a] = fills.get(a, 0) + len(res.trades)
    return fills


# --------------------------------------------------------------------------
# Intraday risk exits
# --------------------------------------------------------------------------

def reduce_now(conn: sqlite3.Connection, agent_id: str, slot: int, ticker: str, fraction: float, d: str,
               price: float, note: str, decision_id: int | None) -> ledger.Trade | None:
    """Shrink a position by `fraction` (1 = close) at `price`. Reduce-only:
    never opens or flips anything, so hard limits always allow it."""
    held = ledger.position_shares(conn, agent_id, slot, ticker)
    if abs(held) < ledger.DUST:
        return None
    delta = -held * max(0.0, min(1.0, fraction))
    o = hardlimits.Order(agent_id, slot, ticker, delta, price, d, "risk_exit", held, 0.0, True)
    errs = hardlimits.check_order(conn, o)
    if errs:                       # can't happen for a reduction; kept as a guard
        hardlimits.reject(conn, agent_id, "order", slot, "order", {"ticker": ticker, "shares": delta}, "; ".join(errs))
        return None
    tr = ledger.execute_trade(conn, agent_id, slot, ticker, delta, d, price, "risk_exit", f"[reduce-only] {note}")
    if tr and decision_id:
        conn.execute("UPDATE trades SET decision_id=? WHERE id=(SELECT MAX(id) FROM trades WHERE agent_id=?)",
                     (decision_id, agent_id))
    return tr

