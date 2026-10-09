"""The ledger: the only code that moves money.

Structure:  agent -> slots (sleeves) -> positions
  * Each slot has its own cash. Slot 0 is the unallocated reserve.
  * shares < 0 is a short. Selling short adds the proceeds to the slot's cash,
    so equity = cash + sum(shares * price) works for longs and shorts alike
    (a short's market value is negative).

Nothing in here checks the competition limits; risk.py does that *before*
anything reaches the ledger.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date as _date, timedelta

from . import costs
from .db import log_event, next_seq, transaction
from .prices import get_price, price_on_or_before

RESERVE_SLOT = 0
DUST = 1e-9  # share counts smaller than this are treated as zero


# --------------------------------------------------------------------------
# Agents, cash, deposits
# --------------------------------------------------------------------------

def create_agent(conn: sqlite3.Connection, agent_id: str, starting_cash: float,
                 on_date: str, model: str | None = None) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO agents(agent_id, model, created_at) VALUES (?,?,?)",
                     (agent_id, model, on_date))
        conn.execute(
            "INSERT INTO slots(agent_id, slot, name, type, allocation, cash) "
            "VALUES (?, ?, 'cash reserve', 'reserve', 0, 0)", (agent_id, RESERVE_SLOT))
        deposit(conn, agent_id, starting_cash, on_date, "starting cash")


def agent_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT agent_id FROM agents ORDER BY agent_id")]


def deposit(conn: sqlite3.Connection, agent_id: str, amount: float, on_date: str,
            note: str = "") -> None:
    """Add money from outside. Goes to the reserve; the next rebalance spreads it."""
    with transaction(conn):
        conn.execute("UPDATE slots SET cash = cash + ? WHERE agent_id=? AND slot=?",
                     (amount, agent_id, RESERVE_SLOT))
        conn.execute("INSERT INTO deposits(seq, agent_id, date, amount, note) VALUES (?,?,?,?,?)",
                     (next_seq(conn), agent_id, on_date, amount, note))


def _ensure_slot(conn: sqlite3.Connection, agent_id: str, slot: int) -> None:
    conn.execute("INSERT OR IGNORE INTO slots(agent_id, slot) VALUES (?,?)", (agent_id, slot))


def slot_cash(conn: sqlite3.Connection, agent_id: str) -> dict[int, float]:
    rows = conn.execute("SELECT slot, cash FROM slots WHERE agent_id=?", (agent_id,))
    return {r["slot"]: r["cash"] for r in rows}


def positions(conn: sqlite3.Connection, agent_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM positions WHERE agent_id=? ORDER BY slot, ticker", (agent_id,)
    ).fetchall()


def position_shares(conn: sqlite3.Connection, agent_id: str, slot: int, ticker: str) -> float:
    row = conn.execute("SELECT shares FROM positions WHERE agent_id=? AND slot=? AND ticker=?",
                       (agent_id, slot, ticker)).fetchone()
    return 0.0 if row is None else row["shares"]


# --------------------------------------------------------------------------
# Valuation & snapshots
# --------------------------------------------------------------------------

@dataclass
class Valuation:
    date: str
    price_field: str
    equity: float
    cash: float
    long_value: float
    short_value: float                 # positive number
    slot_equity: dict[int, float]
    ticker_value: dict[str, float]     # net signed $ per ticker across slots
    prices: dict[str, float]
    stale: list[str] = field(default_factory=list)  # tickers priced from an older bar

    @property
    def gross(self) -> float:
        return sum(abs(v) for v in self.ticker_value.values())


def mark_price(conn: sqlite3.Connection, ticker: str, d: str, fld: str) -> tuple[float, bool]:
    """(price, is_stale). Falls back to the last close if the bar is missing
    (e.g. a trading halt), so equity is always computable."""
    p = get_price(conn, ticker, d, fld)
    if p is not None:
        return p, False
    prev = price_on_or_before(conn, ticker, d, "close")
    if prev is None:
        raise RuntimeError(f"No price at all for {ticker} on or before {d}")
    return prev[1], True


def value_agent(conn: sqlite3.Connection, agent_id: str, d: str, fld: str = "close") -> Valuation:
    cash_by_slot = slot_cash(conn, agent_id)
    slot_eq = dict(cash_by_slot)
    ticker_value: dict[str, float] = {}
    prices: dict[str, float] = {}
    stale: list[str] = []
    long_v = short_v = 0.0
    for p in positions(conn, agent_id):
        t = p["ticker"]
        if t not in prices:
            prices[t], is_stale = mark_price(conn, t, d, fld)
            if is_stale:
                stale.append(t)
        v = p["shares"] * prices[t]
        slot_eq[p["slot"]] = slot_eq.get(p["slot"], 0.0) + v
        ticker_value[t] = ticker_value.get(t, 0.0) + v
        if v >= 0:
            long_v += v
        else:
            short_v += -v
    cash = sum(cash_by_slot.values())
    return Valuation(d, fld, cash + long_v - short_v, cash, long_v, short_v,
                     slot_eq, ticker_value, prices, stale)


def snapshot(conn: sqlite3.Connection, agent_id: str, d: str, kind: str,
             fld: str = "close") -> Valuation:
    assert kind in ("post_trade", "mark", "daily")
    v = value_agent(conn, agent_id, d, fld)
    conn.execute(
        "INSERT INTO snapshots(seq, agent_id, date, kind, price_field, equity, cash, "
        "long_value, short_value, slots_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (next_seq(conn), agent_id, d, kind, fld, v.equity, v.cash, v.long_value,
         v.short_value, json.dumps({str(k): round(x, 6) for k, x in sorted(v.slot_equity.items())})),
    )
    return v


def latest_snapshot(conn: sqlite3.Connection, agent_id: str, kind: str | None = None):
    sql = "SELECT * FROM snapshots WHERE agent_id=?"
    params: list = [agent_id]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    return conn.execute(sql + " ORDER BY seq DESC LIMIT 1", params).fetchone()


def period_return(conn: sqlite3.Connection, agent_id: str, start_snapshot_id: int,
                  end_snapshot_id: int) -> float:
    """Return between two snapshots, with deposits made in between removed.

    Deposits are new money, not performance, so they're subtracted from the
    ending equity before comparing.
    """
    s = conn.execute("SELECT seq, equity FROM snapshots WHERE id=?", (start_snapshot_id,)).fetchone()
    e = conn.execute("SELECT seq, equity FROM snapshots WHERE id=?", (end_snapshot_id,)).fetchone()
    added = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM deposits WHERE agent_id=? AND seq > ? AND seq < ?",
        (agent_id, s["seq"], e["seq"]),
    ).fetchone()[0]
    if s["equity"] <= 0:
        return -1.0
    return (e["equity"] - added) / s["equity"] - 1


# --------------------------------------------------------------------------
# Trades
# --------------------------------------------------------------------------

@dataclass
class Trade:
    slot: int
    ticker: str
    shares: float
    ref_price: float
    fill_price: float
    commission: float
    realized_pnl: float
    reason: str


def execute_trade(conn: sqlite3.Connection, agent_id: str, slot: int, ticker: str,
                  delta: float, d: str, ref_price: float, reason: str, note: str | None = None) -> Trade | None:
    """Change a slot's position by `delta` shares at `ref_price` (+ costs).

    Returns None (and does nothing) if the trade is below the minimum size.
    A single trade may flip a position from long to short or back.
    """
    if abs(delta) < DUST or costs.is_too_small(delta, ref_price):
        return None
    fill = costs.fill_price(ref_price, delta)
    fee = costs.commission(delta, fill)

    row = conn.execute("SELECT shares, avg_price, borrow_paid_through FROM positions "
                       "WHERE agent_id=? AND slot=? AND ticker=?", (agent_id, slot, ticker)).fetchone()
    old = row["shares"] if row else 0.0
    avg = row["avg_price"] if row else 0.0
    paid_through = row["borrow_paid_through"] if row else None
    new = old + delta

    # Realized P&L on the part of the position being closed.
    realized = 0.0
    if old != 0 and (old > 0) != (delta > 0):
        closed = min(abs(delta), abs(old))
        realized = closed * (fill - avg) if old > 0 else closed * (avg - fill)

    if abs(new) < DUST:
        new_avg = 0.0
    elif old == 0 or (old > 0) == (delta > 0):       # opening or adding
        new_avg = (abs(old) * avg + abs(delta) * fill) / (abs(old) + abs(delta))
    elif (old > 0) == (new > 0):                     # reducing, same side
        new_avg = avg
    else:                                            # flipped sides
        new_avg = fill

    # Borrow is charged from the day a short is opened. "Paid through
    # yesterday" means today's close charges exactly one day.
    if new < -DUST:
        if old >= -DUST or paid_through is None:
            paid_through = (_date.fromisoformat(d) - timedelta(days=1)).isoformat()
    else:
        paid_through = None

    conn.execute("UPDATE slots SET cash = cash - ? WHERE agent_id=? AND slot=?",
                 (delta * fill + fee, agent_id, slot))
    if abs(new) < DUST:
        conn.execute("DELETE FROM positions WHERE agent_id=? AND slot=? AND ticker=?",
                     (agent_id, slot, ticker))
        conn.execute("DELETE FROM risk_orders WHERE agent_id=? AND slot=? AND ticker=?",
                     (agent_id, slot, ticker))
    else:
        conn.execute(
            "INSERT INTO positions(agent_id, slot, ticker, shares, avg_price, borrow_paid_through) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(agent_id, slot, ticker) DO UPDATE SET "
            "shares=excluded.shares, avg_price=excluded.avg_price, "
            "borrow_paid_through=excluded.borrow_paid_through",
            (agent_id, slot, ticker, new, new_avg, paid_through),
        )
    conn.execute(
        "INSERT INTO trades(seq, agent_id, slot, date, ticker, shares, ref_price, fill_price, "
        "value, commission, slippage_cost, realized_pnl, reason, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (next_seq(conn), agent_id, slot, d, ticker, delta, ref_price, fill, abs(delta) * fill,
         fee, abs(delta) * abs(fill - ref_price), realized, reason, (note or "")[:500] or None),
    )
    return Trade(slot, ticker, delta, ref_price, fill, fee, realized, reason)


# --------------------------------------------------------------------------
# Rebalance
# --------------------------------------------------------------------------

@dataclass
class SlotTarget:
    """What one slot should look like after the rebalance.

    weights are fractions of the *slot's* equity (negative = short).
    risk_orders: {ticker: {"stop_loss": x, "take_profit": y}}
    """
    slot: int
    allocation: float
    weights: dict[str, float]
    name: str = ""
    type: str = "manual"
    code: str | None = None
    risk_orders: dict[str, dict] = field(default_factory=dict)
    reason: str = ""                                        # the agent's reason for the slot
    notes: dict[str, str] = field(default_factory=dict)     # the agent's reason per position


@dataclass
class RebalanceResult:
    trades: list[Trade]
    skipped: list[str]
    equity_before: float


def rebalance(conn: sqlite3.Connection, agent_id: str, d: str, targets: list[SlotTarget],
              reason: str = "rebalance") -> RebalanceResult:
    """Move the agent to the target portfolio at date d's open prices.

    Must only be called with targets that risk.validate_portfolio accepted.
    Slots not listed in `targets` are closed and their money returns to the
    reserve (slot 0).
    """
    with transaction(conn):
        val = value_agent(conn, agent_id, d, "open")
        equity = val.equity
        by_slot = {t.slot: t for t in targets}
        existing = {r["slot"] for r in conn.execute(
            "SELECT slot FROM slots WHERE agent_id=? AND slot != ?", (agent_id, RESERVE_SLOT))}

        # 1) Update slot metadata; closed slots get allocation 0.
        for s in sorted(existing | set(by_slot)):
            _ensure_slot(conn, agent_id, s)
            t = by_slot.get(s)
            if t is None:
                conn.execute("UPDATE slots SET allocation=0, active=0, updated_at=? "
                             "WHERE agent_id=? AND slot=?", (d, agent_id, s))
            else:
                conn.execute(
                    "UPDATE slots SET allocation=?, name=?, type=?, code=COALESCE(?, code), "
                    "active=1, updated_at=? WHERE agent_id=? AND slot=?",
                    (t.allocation, t.name, t.type, t.code, d, agent_id, s))

        # 2) Re-split cash so each slot's equity equals allocation * total.
        #    Holdings stay put for now; trades in step 3 bring each slot to its
        #    weights, paying for themselves out of the slot's (new) cash.
        holdings: dict[int, float] = {}
        for p in positions(conn, agent_id):
            holdings[p["slot"]] = holdings.get(p["slot"], 0.0) + p["shares"] * val.prices[p["ticker"]]
        allocated = 0.0
        for s in sorted(existing | set(by_slot)):
            target_eq = by_slot[s].allocation * equity if s in by_slot else 0.0
            allocated += target_eq
            conn.execute("UPDATE slots SET cash=? WHERE agent_id=? AND slot=?",
                         (target_eq - holdings.get(s, 0.0), agent_id, s))
        conn.execute("UPDATE slots SET cash=? WHERE agent_id=? AND slot=?",
                     (equity - allocated - holdings.get(RESERVE_SLOT, 0.0), agent_id, RESERVE_SLOT))

        # 3) Work out share changes per (slot, ticker).
        orders: list[tuple[int, str, float, float]] = []   # slot, ticker, delta, open price
        skipped: list[str] = []
        current = {(p["slot"], p["ticker"]): p["shares"] for p in positions(conn, agent_id)}
        wanted: dict[tuple[int, str], float] = {}
        for t in targets:
            for ticker, w in t.weights.items():
                if w != 0:
                    wanted[(t.slot, ticker)] = w * t.allocation * equity  # dollars
        for key in sorted(set(current) | set(wanted)):
            s, ticker = key
            price = get_price(conn, ticker, d, "open")
            if price is None:
                skipped.append(f"{ticker} (slot {s}): no open price on {d}")
                continue
            delta = wanted.get(key, 0.0) / price - current.get(key, 0.0)
            if costs.is_too_small(delta, price):
                continue
            orders.append((s, ticker, delta, price))

        # 4) Cash-freeing trades (sell longs, open shorts) before cash-using
        #    ones, like a real account would have to.
        orders.sort(key=lambda o: (o[2] > 0, o[0], o[1]))
        trades = []
        for s, ticker, delta, price in orders:
            # Cap buys that add long exposure; covering a short must always complete.
            if delta > 0 and position_shares(conn, agent_id, s, ticker) >= 0:
                delta = affordable(conn, agent_id, s, delta, price)
            tr = execute_trade(conn, agent_id, s, ticker, delta, d, price, reason,
                               trade_note(by_slot.get(s), ticker))
            if tr:
                trades.append(tr)

        # 5) Replace risk orders for each targeted slot.
        for t in targets:
            set_risk_orders(conn, agent_id, t.slot, t.risk_orders)
        for s in existing - set(by_slot):
            set_risk_orders(conn, agent_id, s, {})

        for msg in skipped:
            log_event(conn, "warning", f"rebalance:{agent_id}", msg)
    return RebalanceResult(trades, skipped, equity)


def affordable(conn: sqlite3.Connection, agent_id: str, slot: int, shares: float, ref_price: float) -> float:
    """Shrink a buy so it plus its costs fits in the slot's cash. Without
    this, a fully invested slot ends a few cents negative after commission
    and slippage, which is borrowing, and the rules allow no leverage."""
    cash = conn.execute("SELECT cash FROM slots WHERE agent_id=? AND slot=?", (agent_id, slot)).fetchone()[0]
    fill = costs.fill_price(ref_price, shares)
    if shares * fill + costs.commission(shares, fill) <= cash + 1e-9:
        return shares
    fitted = max(0.0, cash - costs.commission(shares, fill)) / fill
    # The commission of the smaller order may differ slightly; one more pass settles it.
    return max(0.0, cash - costs.commission(fitted, fill)) / fill


def trade_note(target: SlotTarget | None, ticker: str) -> str:
    """Why a rebalance trade happened, in the agent's own words when it gave them."""
    if target is None:
        return "Slot closed: no longer part of the plan."
    if ticker not in target.weights or not target.weights.get(ticker):
        return f"Removed from \"{target.name}\": not in this week's plan."
    own = target.notes.get(ticker)
    if own:
        return own
    return f"{target.name}: {target.reason}" if target.reason else target.name


def set_risk_orders(conn: sqlite3.Connection, agent_id: str, slot: int,
                    orders: dict[str, dict]) -> None:
    conn.execute("DELETE FROM risk_orders WHERE agent_id=? AND slot=?", (agent_id, slot))
    for ticker, o in sorted(orders.items()):
        sl, tp = o.get("stop_loss"), o.get("take_profit")
        if sl is None and tp is None:
            continue
        conn.execute("INSERT INTO risk_orders(agent_id, slot, ticker, stop_loss, take_profit) "
                     "VALUES (?,?,?,?,?)", (agent_id, slot, ticker, sl, tp))


# --------------------------------------------------------------------------
# Corporate actions
# --------------------------------------------------------------------------

def apply_price_adjustment(conn: sqlite3.Connection, ticker: str, factor: float, d: str) -> None:
    """Called by prices.update_prices when a ticker's history was rescaled.

    factor = new_price / old_price. Dividing shares by it keeps every
    position's value unchanged at the moment of the rescale. For a split
    that's the usual share multiplication; for a dividend it effectively
    reinvests the dividend (longs receive it, shorts pay it).
    """
    if factor <= 0:
        raise ValueError(f"Bad adjustment factor {factor} for {ticker}")
    conn.execute("UPDATE positions SET shares = shares / ?, avg_price = avg_price * ? WHERE ticker=?",
                 (factor, factor, ticker))
    conn.execute("UPDATE risk_orders SET stop_loss = stop_loss * ?, take_profit = take_profit * ? "
                 "WHERE ticker=?", (factor, factor, ticker))
    conn.execute("INSERT INTO corporate_actions(seq, date, ticker, factor, note) VALUES (?,?,?,?,?)",
                 (next_seq(conn), d, ticker, factor,
                  "split" if abs(factor - 1) > 0.2 else "dividend/adjustment"))
