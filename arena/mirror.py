"""Optional: copy the league into Alpaca *paper* accounts so you can watch
it there too. Runs after each morning's `execute`. MIRROR_MODE in .env:

  off     nothing is sent to Alpaca (default)
  leader  your paper account copies whichever agent leads
  each      every agent gets its own paper account: Claude's uses
            ALPACA_MIRROR_CLAUDE_KEY/SECRET (default: your main Alpaca keys),
            ChatGPT's uses ALPACA_MIRROR_GPT_KEY/SECRET (a second paper account)
  combined  all agents share ONE paper account (your main keys), each owning
            an equal share of it. Alpaca only sees the total per stock, so the
            program keeps its own record of whose shares are whose
            (mirror_holdings) and labels every order with the agent(s) behind
            it, in Alpaca (the order's client ID) and on the dashboard.

The virtual league allows things a real broker doesn't, so the mirror
adapts and logs every difference in the mirror_log table:
  * fractional shares are fine for longs, but shorts must be whole shares:
    shorts are rounded toward zero, and shorts under 1 share are skipped;
  * a position can't flip long <-> short in one order: it's closed first,
    then reopened on the other side;
  * order sizes follow MIRROR_SIZING (see sizing()).

MIRROR_SIZING in .env:

  scaled  (default) the paper account holds the same *weights* as the
          league, sized to its own equity: an agent with $1,000 copied
          into a $50,000 share moves 50x the dollars
  exact   the paper account holds the league's own share counts, so its
          dollar gains and losses match the website. The rest of the
          account sits as cash. At league size more orders hit the limits
          above (shorts under 1 share, orders under $1).
"""
from __future__ import annotations

import math
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from . import ledger, scoring
from .config import MissingKeyError, require_env
from .db import log_event, transaction
from .prices import get_price, price_on_or_before

MIN_ORDER_VALUE = 1.0      # Alpaca's minimum notional for fractional orders



class Broker(Protocol):
    def equity(self) -> float: ...
    def positions(self) -> dict[str, float]: ...          # signed quantities
    def submit(self, ticker: str, qty: float, label: str = "") -> str: ...  # signed; returns an order id


def mode() -> str:
    """off | leader | each | combined (read at call time, so the Setup page's changes apply at once)."""
    m = os.getenv("MIRROR_MODE", "").strip().lower()
    if m in ("off", "leader", "each", "combined"):
        return m
    return "leader" if os.getenv("MIRROR_PAPER", "0") == "1" else "off"   # older setting


def sizing() -> str:
    """scaled | exact (read at call time, like mode())."""
    s = os.getenv("MIRROR_SIZING", "").strip().lower()
    return s if s in ("scaled", "exact") else "scaled"


def keys_for(agent_id: str) -> tuple[str, str]:
    key = os.getenv(f"ALPACA_MIRROR_{agent_id.upper()}_KEY", "").strip()
    secret = os.getenv(f"ALPACA_MIRROR_{agent_id.upper()}_SECRET", "").strip()
    if key and secret:
        return key, secret
    if agent_id == "claude":       # Claude's account defaults to your main Alpaca keys
        return require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY")
    raise MissingKeyError(f"Paper-account keys for {agent_id} aren't set (Setup, step 6).")


class AlpacaPaperBroker:
    def __init__(self, key: str | None = None, secret: str | None = None):
        from alpaca.trading.client import TradingClient
        self.client = TradingClient(key or require_env("ALPACA_API_KEY"),
                                    secret or require_env("ALPACA_SECRET_KEY"),
                                    paper=True)   # never a live account

    def account_id(self) -> str:
        return str(self.client.get_account().account_number)

    def equity(self) -> float:
        return float(self.client.get_account().equity)

    def positions(self) -> dict[str, float]:
        out = {}
        for p in self.client.get_all_positions():
            q = float(p.qty)
            if str(getattr(p.side, "value", p.side)).lower() == "short" and q > 0:
                q = -q
            out[p.symbol] = q
        return out

    def submit(self, ticker: str, qty: float, label: str = "") -> str:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        order = self.client.submit_order(MarketOrderRequest(
            symbol=ticker, qty=round(abs(qty), 6), side=OrderSide.BUY if qty > 0 else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,   # fractional orders must be DAY orders
            # Shown in Alpaca's order details: which agent(s) this order is for.
            client_order_id=f"arena-{label or 'mirror'}-{uuid.uuid4().hex[:10]}"))
        return str(order.id)


@dataclass
class MirrorLine:
    ticker: str
    virtual_weight: float
    ideal_qty: float
    target_qty: float
    current_qty: float
    orders: list[float] = field(default_factory=list)
    note: str = ""


def leader(conn: sqlite3.Connection) -> str | None:
    """Most points; ties broken by cumulative return; still tied -> nobody."""
    rows = [(scoring.points(conn, a), scoring.cumulative_return(conn, a), a) for a in ledger.agent_ids(conn)]
    if not rows:
        return None
    rows.sort(reverse=True)
    if len(rows) > 1 and rows[0][:2] == rows[1][:2]:
        return None
    return rows[0][2]


def plan(weights: dict[str, float], prices: dict[str, float], paper_equity: float,
         current: dict[str, float]) -> list[MirrorLine]:
    """Orders that move the paper account to `weights` (fractions of equity)."""
    lines = []
    for t in sorted(set(weights) | set(current)):
        w = weights.get(t, 0.0)
        cur = current.get(t, 0.0)
        px = prices.get(t)
        if not px:
            lines.append(MirrorLine(t, w, 0.0, cur, cur, note="no price; left unchanged"))
            continue
        ideal = w * paper_equity / px
        note = ""
        if ideal < 0:
            target = -math.floor(-ideal)                 # whole shares, toward zero
            if target == 0:
                note = f"short of {ideal:.3f} sh is under 1 share: skipped"
            elif target != ideal:
                note = f"short rounded {ideal:.3f} -> {target} sh (no fractional shorts)"
        else:
            target = round(ideal, 6)
        line = MirrorLine(t, w, ideal, target, cur, note=note)
        if abs(target - cur) * px < MIN_ORDER_VALUE:
            lines.append(line)
            continue
        if cur and target and (cur > 0) != (target > 0):
            line.orders = [-cur, target]                  # close, then open the other side
            line.note = (line.note + "; " if line.note else "") + "flipped side in two orders"
        else:
            line.orders = [target - cur]
        lines.append(line)
    return lines


def sync(conn: sqlite3.Connection, d: str, broker: Broker | None = None, dry_run: bool = False,
         agent: str | None = None) -> list[MirrorLine]:
    """Mirror one agent (default: the leader) into a paper account at date d's prices."""
    who = agent or leader(conn)
    if who is None:
        log_event(conn, "info", "mirror", f"{d}: no single leader; paper account left unchanged")
        return []
    val = ledger.value_agent(conn, who, d, "open")
    if val.equity <= 0:
        return []
    weights = {t: v / val.equity for t, v in val.ticker_value.items()}
    broker = broker or AlpacaPaperBroker()
    current = broker.positions()
    prices = {}
    for t in set(weights) | set(current):
        p = get_price(conn, t, d, "open") or (price_on_or_before(conn, t, d) or (None, None))[1]
        if p:
            prices[t] = p
    # exact: size to the agent's own equity, so weight * equity / price = its share count
    lines = plan(weights, prices, val.equity if sizing() == "exact" else broker.equity(), current)
    if dry_run:
        return lines
    # Sells (and short opens) first so buys have the cash.
    ids: dict[str, list[str]] = {}
    for line in sorted(lines, key=lambda l: (l.orders[0] > 0) if l.orders else True):
        for q in line.orders:
            try:
                ids.setdefault(line.ticker, []).append(broker.submit(line.ticker, q))
            except Exception as e:  # noqa: BLE001 - one rejected order shouldn't stop the rest
                line.note = (line.note + "; " if line.note else "") + f"order rejected: {e}"[:300]
                break
    with transaction(conn):
        for l in lines:
            if l.orders or l.note:
                conn.execute(
                    "INSERT INTO mirror_log(date, leader, ticker, virtual_weight, ideal_qty, target_qty, "
                    "current_qty, orders, order_ids, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (d, who, l.ticker, l.virtual_weight, l.ideal_qty, l.target_qty, l.current_qty,
                     str([round(q, 6) for q in l.orders]), ",".join(ids.get(l.ticker, [])), l.note))
        log_event(conn, "info", "mirror", f"{d}: mirrored {who}; {sum(len(l.orders) for l in lines)} order(s)")
        conn.execute("INSERT INTO mirror_runs(date, mode, equity, orders) VALUES (?,?,?,?)",
                     (d, mode(), broker.equity(), sum(len(l.orders) for l in lines)))
    return lines


def brokers_for_each(agent_ids: list[str], skipped: list[str] | None = None) -> dict[str, "AlpacaPaperBroker"]:
    """One paper account per agent, refusing if two agents share one (their
    positions would mix and both mirrors would fight over them). Agents with
    no account set up yet are left out (and named in `skipped`)."""
    brokers = {}
    for a in agent_ids:
        try:
            brokers[a] = AlpacaPaperBroker(*keys_for(a))
        except MissingKeyError:
            if skipped is None:
                raise
            skipped.append(a)
    seen: dict[str, str] = {}
    for a, b in brokers.items():
        acct = b.account_id()
        if acct in seen:
            raise ValueError(f"{seen[acct]} and {a} point to the same Alpaca paper account. "
                             f"Create a second paper account for {a} and use its keys.")
        seen[acct] = a
    return brokers


def sync_all(conn: sqlite3.Connection, d: str, dry_run: bool = False,
             brokers: dict | None = None) -> dict[str, list[MirrorLine]]:
    """Run the mirror the way MIRROR_MODE says. Returns {agent: lines}."""
    m = mode()
    if m == "off":
        return {}
    if m == "combined":
        return sync_combined(conn, d, (brokers or {}).get("combined"), dry_run)
    if m == "leader":
        who = leader(conn)
        return {who: sync(conn, d, (brokers or {}).get(who), dry_run, agent=who)} if who else {}
    agents = ledger.agent_ids(conn)
    skipped: list[str] = []
    brokers = brokers or brokers_for_each(agents, skipped)
    if skipped:
        log_event(conn, "info", "mirror", f"{d}: no paper account set up for {', '.join(skipped)}; skipped")
    return {a: sync(conn, d, brokers[a], dry_run, agent=a) for a in agents if a in brokers}


def check_accounts(agent_ids: list[str]) -> dict:
    """For the Setup page: can each agent's paper account be reached?"""
    out = {}
    skipped: list[str] = []
    try:
        brokers = brokers_for_each(agent_ids, skipped)
    except (MissingKeyError, ValueError) as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:  # noqa: BLE001 - show connection problems in plain words
        return {"ok": False, "message": f"Couldn't reach Alpaca: {str(e)[:200]}"}
    for a, b in brokers.items():
        out[a] = f"account …{b.account_id()[-4:]}, equity ${b.equity():,.0f}"
    for a in skipped:
        out[a] = "no paper account yet (add its keys to copy it too)"
    return {"ok": bool(brokers), "message": "; ".join(f"{a}: {v}" for a, v in out.items())}


# --------------------------------------------------------------------------
# Combined: every agent in one paper account
# --------------------------------------------------------------------------

def _attributed(conn) -> dict[tuple[str, str], float]:
    """Whose shares are in the shared account, by our own records. The first
    time, fall back to what earlier mirror runs put there per agent."""
    rows = conn.execute("SELECT agent_id, ticker, qty FROM mirror_holdings").fetchall()
    if rows:
        return {(r["agent_id"], r["ticker"]): r["qty"] for r in rows}
    out = {}
    for r in conn.execute(
            "SELECT leader, ticker, target_qty FROM mirror_log m WHERE id = (SELECT MAX(id) FROM mirror_log "
            "WHERE leader = m.leader AND ticker = m.ticker)"):
        if r["target_qty"]:
            out[(r["leader"], r["ticker"])] = r["target_qty"]
    return out


def sync_combined(conn: sqlite3.Connection, d: str, broker: Broker | None = None,
                  dry_run: bool = False) -> dict[str, list[MirrorLine]]:
    """One paper account for all agents, split equally (or, with
    MIRROR_SIZING=exact, each agent's own league equity). Sends ONE net order
    per ticker (so the agents never trade against each other inside the
    account) and records each agent's part of it."""
    agents = ledger.agent_ids(conn)
    if not agents:
        return {}
    broker = broker or AlpacaPaperBroker()
    equity = broker.equity()
    exact = sizing() == "exact"
    current = broker.positions()
    prev = _attributed(conn)
    weights: dict[tuple[str, str], float] = {}
    budget: dict[str, float] = {}
    for a in agents:
        val = ledger.value_agent(conn, a, d, "open")
        budget[a] = val.equity if exact else equity / len(agents)
        if val.equity > 0:
            for t, v in val.ticker_value.items():
                weights[(a, t)] = v / val.equity
    tickers = sorted({t for _, t in weights} | set(current) | {t for _, t in prev})
    lines: dict[str, list[MirrorLine]] = {a: [] for a in agents}
    plan_orders: list[tuple[str, list[float], str]] = []
    new_holdings: dict[tuple[str, str], float] = {}
    for t in tickers:
        px = get_price(conn, t, d, "open") or (price_on_or_before(conn, t, d) or (None, None))[1]
        if not px:
            continue
        ideal = {a: weights.get((a, t), 0.0) * budget[a] / px for a in agents}
        net = sum(ideal.values())
        net_target = -math.floor(-net) if net < 0 else round(net, 6)   # whole-share shorts
        cur = current.get(t, 0.0)
        note = f"account short rounded {net:.3f} -> {net_target} sh" if net < 0 and net_target != net else ""
        qtys: list[float] = []
        if abs(net_target - cur) * px >= MIN_ORDER_VALUE:
            qtys = [-cur, net_target] if cur and net_target and (cur > 0) != (net_target > 0) else [net_target - cur]
        movers = [a for a in agents if abs(ideal[a] - prev.get((a, t), 0.0)) * px >= 0.01]
        label = "+".join(movers) or "rebalance"
        for a in agents:
            new_holdings[(a, t)] = ideal[a]
            delta = ideal[a] - prev.get((a, t), 0.0)
            if a in movers or (weights.get((a, t)) and qtys):
                shared = f"shared order with {', '.join(m for m in movers if m != a)}" if len(movers) > 1 else ""
                lines[a].append(MirrorLine(t, weights.get((a, t), 0.0), ideal[a], ideal[a], prev.get((a, t), 0.0),
                                           [delta] if a in movers else [], "; ".join(x for x in (note, shared) if x)))
        if qtys:
            plan_orders.append((t, qtys, label))
    if dry_run:
        return lines
    ids: dict[str, list[str]] = {}
    for t, qtys, label in sorted(plan_orders, key=lambda o: o[1][0] > 0):   # sells first
        for q in qtys:
            try:
                ids.setdefault(t, []).append(broker.submit(t, q, label))
            except Exception as e:  # noqa: BLE001 - one rejected order shouldn't stop the rest
                for a in agents:
                    for l in lines[a]:
                        if l.ticker == t:
                            l.note = (l.note + "; " if l.note else "") + f"order rejected: {e}"[:300]
                break
    with transaction(conn):
        conn.execute("DELETE FROM mirror_holdings")
        conn.executemany("INSERT INTO mirror_holdings(agent_id, ticker, qty) VALUES (?,?,?)",
                         [(a, t, q) for (a, t), q in new_holdings.items() if abs(q) > 1e-9])
        for a, ls in lines.items():
            for l in ls:
                conn.execute(
                    "INSERT INTO mirror_log(date, leader, ticker, virtual_weight, ideal_qty, target_qty, "
                    "current_qty, orders, order_ids, note) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (d, a, l.ticker, l.virtual_weight, l.ideal_qty, l.target_qty, l.current_qty,
                     str([round(q, 6) for q in l.orders]), ",".join(ids.get(l.ticker, [])), l.note))
        conn.execute("INSERT INTO mirror_runs(date, mode, equity, orders) VALUES (?,?,?,?)",
                     (d, "combined", equity, sum(len(v) for v in ids.values())))
        log_event(conn, "info", "mirror", f"{d}: combined account synced; {sum(len(v) for v in ids.values())} order(s)")
    return lines


# --------------------------------------------------------------------------
# Daily reconciliation (scheduler job "reconcile")
# --------------------------------------------------------------------------

def compare_positions(actual: dict[str, float], expected: dict[str, float]) -> list[dict]:
    """Differences between the paper account and the league's records.

    A short can only be held in whole shares, so for a short the account may
    hold the recorded quantity rounded towards zero (e.g. -124 for -124.38).
    Anything else above a tiny tolerance is a mismatch."""
    out = []
    for t in sorted(set(actual) | set(expected)):
        a, e = actual.get(t, 0.0), expected.get(t, 0.0)
        if abs(a - e) < 1e-4:
            continue
        if e < 0 and abs(a - int(e)) < 1e-6:
            continue                       # whole-share rounding of a short
        out.append({"ticker": t, "alpaca": a, "expected": round(e, 6), "diff": round(a - e, 6)})
    return out


def reconcile(conn: sqlite3.Connection, broker: Broker | None = None) -> dict:
    """Compare the paper account with what the league says it should hold.
    combined mode: per ticker, the sum of every agent's recorded share
    (mirror_holdings). Returns {'checked': bool, 'mismatches': [...], ...}."""
    m = mode()
    if m != "combined":
        return {"checked": False, "mode": m,
                "note": "Reconciliation is built for MIRROR_MODE=combined (the mode in use)."}
    if broker is None:
        key, secret = keys_for(ledger.agent_ids(conn)[0])
        broker = AlpacaPaperBroker(key, secret)
    expected: dict[str, float] = {}
    for r in conn.execute("SELECT ticker, qty FROM mirror_holdings"):
        expected[r[0]] = expected.get(r[0], 0.0) + r[1]
    actual = broker.positions()
    mism = compare_positions(actual, expected)
    return {"checked": True, "mode": m, "tickers": len(set(actual) | set(expected)), "mismatches": mism}
