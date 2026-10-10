"""Read-only data behind the V3 dashboard (web/index.html).

Everything here is computed from what the league already stores: the trade
log, the accepted decisions, slots, scores and prices. Nothing is written.

Reveal rule: what an agent decided for a week becomes visible once that
week has been EXECUTED (its positions are live and on the dashboard anyway).
A decision that is still waiting for Monday's open stays hidden, so the
pending plan is never shown before it trades. Reports remain scored-only.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date

from .. import ledger, scoring
from ..config import SETTINGS
from ..prices import get_price
from .queries import _r, display_mark, spy_week_return

EPS = 1e-9


# --------------------------------------------------------------------------
# Decisions that may be shown
# --------------------------------------------------------------------------

def _shown_decisions(conn, agent_id: str, limit: int = 2) -> list[dict]:
    """Accepted decisions of executed (or scored) weeks, newest first."""
    rows = conn.execute(
        "SELECT t.week_no, t.decision_json, w.decision_date, w.exec_date, w.status FROM agent_turns t "
        "JOIN weeks w ON w.week_no = t.week_no WHERE t.agent_id=? AND t.status='accepted' "
        "AND w.status IN ('executed', 'scored') ORDER BY t.week_no DESC LIMIT ?", (agent_id, limit)).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r["decision_json"] or "{}")
        except json.JSONDecodeError:
            d = {}
        out.append({"week": r["week_no"], "decision_date": r["decision_date"], "exec_date": r["exec_date"],
                    "status": r["status"], "decision": d})
    return out


def _change_tag(slot: dict, prev: dict | None) -> str:
    """KEPT / RETUNED / NEW / REPLACED, comparing with last week's decision."""
    if prev is None:
        return "NEW"
    if (prev.get("name") or "").strip().lower() != (slot.get("name") or "").strip().lower():
        return "REPLACED"
    same = (prev.get("type") == slot.get("type") and abs((prev.get("allocation") or 0) - (slot.get("allocation") or 0)) < 1e-6
            and (prev.get("positions") or {}) == (slot.get("positions") or {}))
    return "KEPT" if same else "RETUNED"


def strategies(conn, agent_id: str) -> dict:
    """Current strategies (slots) with their thesis, allocation and returns."""
    d, fld = display_mark(conn)
    val = ledger.value_agent(conn, agent_id, d, fld) if d else None
    eq = val.equity if val else 0.0
    shown = _shown_decisions(conn, agent_id)
    cur = {int(s.get("slot", 0)): s for s in (shown[0]["decision"].get("slots", []) if shown else [])}
    prev = {int(s.get("slot", 0)): s for s in (shown[1]["decision"].get("slots", []) if len(shown) > 1 else [])}
    last_week = conn.execute("SELECT MAX(week_no) FROM slot_scores WHERE agent_id=?", (agent_id,)).fetchone()[0]
    out = []
    for s in conn.execute("SELECT * FROM slots WHERE agent_id=? AND active=1 AND slot>0 ORDER BY slot", (agent_id,)):
        dec = cur.get(s["slot"], {})
        lw = conn.execute("SELECT weekly_return FROM slot_scores WHERE agent_id=? AND slot=? AND week_no=?",
                          (agent_id, s["slot"], last_week)).fetchone() if last_week else None
        s_eq = val.slot_equity.get(s["slot"], 0.0) if val else 0.0
        out.append({
            "slot": s["slot"], "name": s["name"] or dec.get("name") or f"Slot {s['slot']}", "type": s["type"],
            "thesis": dec.get("reason") or "",
            "allocation": _r(s_eq / eq) if eq > 0 else None, "target_allocation": _r(s["allocation"]),
            "since_inception": _r(scoring.slot_since_inception(conn, agent_id, s["slot"])),
            "last_week": _r(lw[0]) if lw else None,
            "change": _change_tag(dec, prev.get(s["slot"])) if dec else "KEPT",
        })
    out.sort(key=lambda x: -(x["allocation"] or 0))
    return {"agent": agent_id, "as_of": d, "equity": _r(eq, 2), "cash": _r(val.cash, 2) if val else None,
            "cash_pct": _r(val.cash / eq) if val and eq > 0 else None,
            "long_pct": _r(val.long_value / eq) if val and eq > 0 else None,
            "short_pct": _r(val.short_value / eq) if val and eq > 0 else None,
            "decided_week": shown[0]["week"] if shown else None, "strategies": out}


def reasoning(conn, agent_id: str) -> dict:
    """The agent's latest shown market view and notes to self."""
    shown = _shown_decisions(conn, agent_id, limit=1)
    nxt = conn.execute("SELECT decision_date FROM weeks WHERE status IN ('deciding', 'decided') "
                       "ORDER BY week_no DESC LIMIT 1").fetchone()
    items = []
    if shown:
        dd = shown[0]["decision"]
        when = date.fromisoformat(shown[0]["decision_date"])
        if dd.get("market_view"):
            items.append({"when": f"WEEK {shown[0]['week']} DECISION · DATA TO {when:%a %b %d}".upper(),
                          "text": dd["market_view"]})
        if dd.get("notes_to_self"):
            items.append({"when": "NOTE TO SELF", "text": dd["notes_to_self"]})
    return {"agent": agent_id, "week": shown[0]["week"] if shown else None, "items": items,
            "pending_decision": bool(nxt), "next_decision": "Sat 10:00 ET"}


# --------------------------------------------------------------------------
# Holdings with their strategy
# --------------------------------------------------------------------------

def holdings_strategy(conn, agent_id: str) -> dict[int, str]:
    return {r["slot"]: r["name"] for r in conn.execute("SELECT slot, name FROM slots WHERE agent_id=?", (agent_id,))}


# --------------------------------------------------------------------------
# Completed trades: rebuilt from the trade log
# --------------------------------------------------------------------------

def _trading_days(conn, a: str, b: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM calendar WHERE date > ? AND date <= ?", (a, b)).fetchone()[0]


def _why(t) -> str:
    if t["reason"] == "stop_loss":
        return "Stop-loss hit"
    if t["reason"] == "take_profit":
        return "Take-profit hit"
    if t["reason"] == "forced_cover":
        return "Forced cover (short limit)"
    return (t["note"] or "Rebalance").strip()


def closed_trades(conn, limit: int = 100) -> list[dict]:
    """Positions that were fully closed: entry -> exit, how long held, net
    P&L after commissions and borrow fees, and why it closed. A position is
    one (agent, slot, ticker) from the moment it opens until it is back at 0
    (a long that flips to short closes and opens a new one)."""
    actions: dict[str, list[tuple[int, float]]] = {}
    for r in conn.execute("SELECT ticker, seq, factor FROM corporate_actions ORDER BY seq"):
        actions.setdefault(r["ticker"], []).append((r["seq"], r["factor"]))
    open_: dict[tuple, dict] = {}
    done: list[dict] = []

    def adjust(lot, ticker, upto_seq):
        for seq, f in actions.get(ticker, []):
            if lot["seq"] < seq <= upto_seq:
                lot["shares"] /= f                # a split/adjustment rescales the held shares
        lot["seq"] = upto_seq

    for t in conn.execute("SELECT * FROM trades ORDER BY seq"):
        key = (t["agent_id"], t["slot"], t["ticker"])
        lot = open_.get(key)
        if lot is not None:
            adjust(lot, t["ticker"], t["seq"])
        qty, fill = t["shares"], t["fill_price"]
        if lot is None or abs(lot["shares"]) < EPS:
            lot = open_[key] = {"shares": 0.0, "seq": t["seq"], "open_date": t["date"], "side": "LONG" if qty > 0 else "SHORT",
                                "in_qty": 0.0, "in_value": 0.0, "out_qty": 0.0, "out_value": 0.0,
                                "realized": 0.0, "fees": 0.0, "open_seq": t["seq"]}
        same_dir = (lot["shares"] >= 0 and qty > 0) or (lot["shares"] <= 0 and qty < 0)
        if abs(lot["shares"]) < EPS or same_dir:
            lot["in_qty"] += abs(qty)
            lot["in_value"] += abs(qty) * fill
            lot["shares"] += qty
            lot["fees"] += t["commission"]
            continue
        # Reducing (maybe flipping) the position.
        sign = 1.0 if qty > 0 else -1.0
        closing = min(abs(qty), abs(lot["shares"]))
        share = closing / abs(qty)
        rest = qty - sign * closing               # left over after closing: opens a new position
        lot["out_qty"] += closing
        lot["out_value"] += closing * fill
        lot["realized"] += t["realized_pnl"]
        lot["fees"] += t["commission"] * share
        lot["shares"] += sign * closing
        if abs(lot["shares"]) < 1e-7:
            borrow = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM borrow_fees WHERE agent_id=? AND slot=? "
                                  "AND ticker=? AND seq > ? AND seq <= ?",
                                  (t["agent_id"], t["slot"], t["ticker"], lot["open_seq"], t["seq"])).fetchone()[0]
            entry = lot["in_value"] / lot["in_qty"] if lot["in_qty"] else None
            exit_ = lot["out_value"] / lot["out_qty"] if lot["out_qty"] else None
            done.append({"agent": t["agent_id"], "slot": t["slot"], "ticker": t["ticker"], "side": lot["side"],
                         "opened": lot["open_date"], "closed": t["date"], "entry": _r(entry, 4), "exit": _r(exit_, 4),
                         "held_days": _trading_days(conn, lot["open_date"], t["date"]),
                         "net_pnl": _r(lot["realized"] - lot["fees"] - borrow, 2),
                         "why": _why(t), "reason": t["reason"], "seq": t["seq"]})
            del open_[key]
            if abs(rest) > EPS:                   # flipped: the rest opens a new position
                open_[key] = {"shares": rest, "seq": t["seq"], "open_date": t["date"],
                              "side": "LONG" if rest > 0 else "SHORT", "in_qty": abs(rest),
                              "in_value": abs(rest) * fill, "out_qty": 0.0, "out_value": 0.0, "realized": 0.0,
                              "fees": t["commission"] * (1 - share), "open_seq": t["seq"]}
    done.sort(key=lambda x: -x["seq"])
    for x in done:
        x.pop("seq")
    return done[:max(1, min(int(limit), 500))]


# --------------------------------------------------------------------------
# Ticker strip and leaderboard extras
# --------------------------------------------------------------------------

def ticker_strip(conn) -> list[dict]:
    """Every held ticker: last mark, change vs the previous close, who holds it."""
    d, fld = display_mark(conn)
    held: dict[str, set] = {}
    for r in conn.execute("SELECT agent_id, ticker FROM positions WHERE ABS(shares) > 0"):
        held.setdefault(r["ticker"], set()).add(r["agent_id"])
    out = []
    for t in sorted(held):
        last = get_price(conn, t, d, fld) if d else None
        prev = conn.execute("SELECT close FROM prices WHERE ticker=? AND date < ? ORDER BY date DESC LIMIT 1",
                            (t, d)).fetchone() if d else None
        if last is None:
            row = conn.execute("SELECT close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 1", (t,)).fetchone()
            last = row[0] if row else None
        out.append({"ticker": t, "last": _r(last, 4), "prev_close": _r(prev[0], 4) if prev else None,
                    "change": _r(last / prev[0] - 1) if last and prev and prev[0] else None,
                    "agents": sorted(held[t])})
    return out


def leaderboard_extras(conn) -> dict:
    """Per agent: win rate and best strategy; plus SPY this week."""
    out = {}
    for a in ledger.agent_ids(conn):
        scored = conn.execute("SELECT COUNT(*), COALESCE(SUM(point), 0) FROM scores WHERE agent_id=?", (a,)).fetchone()
        best = None
        for s in conn.execute("SELECT slot, name FROM slots WHERE agent_id=? AND slot>0", (a,)):
            r = scoring.slot_since_inception(conn, a, s["slot"])
            if r is not None and (best is None or r > best["return"]):
                best = {"name": s["name"], "return": _r(r)}
        out[a] = {"weeks_scored": scored[0], "wins": scored[1],
                  "win_rate": _r(scored[1] / scored[0]) if scored[0] else None, "best_strategy": best}
    # SPY over the current week: from the latest execution open to now.
    wk = conn.execute("SELECT week_no, exec_date FROM weeks WHERE status IN ('executed', 'scored') "
                      "ORDER BY week_no DESC LIMIT 1").fetchone()
    spy_week = None
    if wk:
        d, fld = display_mark(conn)
        a, b = get_price(conn, "SPY", wk["exec_date"], "open"), get_price(conn, "SPY", d, fld) if d else None
        spy_week = _r(b / a - 1) if a and b else None
    last_scored = conn.execute("SELECT MAX(week_no) FROM scores").fetchone()[0]
    return {"agents": out, "spy_this_week": spy_week,
            "spy_last_scored_week": spy_week_return(conn, last_scored) if last_scored else None}


def overview(conn: sqlite3.Connection) -> dict:
    """One call for the page's top half."""
    wk = conn.execute("SELECT week_no, status, decision_date, exec_date FROM weeks ORDER BY week_no DESC LIMIT 1").fetchone()
    first = conn.execute("SELECT MIN(date) FROM snapshots WHERE kind='post_trade'").fetchone()[0]
    spy_base = get_price(conn, "SPY", first, "open") if first else None
    return {"week": dict(wk) if wk else None, "since": first, "starting_cash": SETTINGS.starting_cash,
            "spy_base": _r(spy_base, 4),          # SPY at the first trade: the benchmark's $1,000 start
            "bonus_every_weeks": SETTINGS.bonus_every_weeks, "bonus_amount": SETTINGS.bonus_amount,
            "ticker": ticker_strip(conn), "leaderboard": leaderboard_extras(conn),
            "limits": {"max_gross": SETTINGS.limits.max_gross, "max_short_total": SETTINGS.limits.max_short_total,
                       "max_long_per_ticker": SETTINGS.limits.max_long_per_ticker,
                       "max_short_per_ticker": SETTINGS.limits.max_short_per_ticker}}
