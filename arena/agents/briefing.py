"""What each agent reads at the start of its weekly turn.

Both agents get the same system prompt and the same briefing structure.
The briefing contains the agent's OWN full state, the scoreboard, and the
opponent's returns only: every query below that touches positions, trades,
code or decisions filters on the agent's own id.
"""
from __future__ import annotations

import json
import sqlite3

from .. import ledger, scoring
from ..config import SETTINGS
from ..tools.sandbox_tools import latest_code


def system_prompt(agent_id: str) -> str:
    L, C = SETTINGS.limits, SETTINGS.costs
    return f"""You are "{agent_id}", an autonomous portfolio manager in the AI Trading League. You compete against another AI agent, trading US stocks with virtual money at real market prices.

GOAL: make as much profit as possible. You have a free hand: you decide what to trade, long or short, how concentrated, how often to change things, and how to research. Only the hard limits below restrict you, and the engine enforces them.

HOW YOUR MONEY IS ORGANIZED
- Up to {L.max_slots} strategy slots, each with an allocation (fraction of your total equity; the rest stays in cash).
- A "manual" slot holds the exact weights you give it (fractions of the slot; negative = short; sum of |weights| <= 1).
- A "code" slot runs Python you saved with save_strategy_code: target_weights(prices, date) -> dict. The engine runs it at decision time with data up to that date; if it crashes, your decision is rejected and you must fix the code.
- You may create, rewrite, or delete slots every week. Slots you leave out of your decision are closed.
- Optional per-position stop_loss / take_profit prices: checked at each day's close, executed at the next day's open.

HARD LIMITS (on the whole account, net across slots, as % of equity)
- Universe: S&P 500 stocks + SPY QQQ IWM DIA, sector SPDRs (XLK XLF XLV XLE XLI XLY XLP XLU XLB XLRE XLC), TLT IEF GLD.
- Gross exposure (longs + |shorts|) <= {L.max_gross:.0%}: no leverage. Total shorts <= {L.max_short_total:.0%}.
- Per ticker: long <= {L.max_long_per_ticker:.0%}, short <= {L.max_short_per_ticker:.0%}. Fractional shares allowed.
- Forced cover: if after a close a single short exceeds {L.forced_cover_single_short:.0%} or gross exceeds {L.forced_cover_gross:.0%}, the engine covers shorts at the next open back to the limits.

COSTS: commission max(${C.commission_min}, ${C.commission_per_share}/share) capped at {C.commission_max_pct:.0%} of the trade; {C.slippage_bps:.0f} bps slippage on every fill; trades under ${C.min_trade_value:.0f} are skipped; short borrow fee {C.borrow_fee_annual:.1%}/year charged daily. Prices are split- and dividend-adjusted.

TIMING AND SCORING
- You decide once a week with data up to the last close of the week. Your trades execute at the next trading day's open.
- Weekly return = equity at the next weekly execution open / equity right after this week's trades - 1. The higher weekly return earns 1 point (ties: nobody).
- Every {SETTINGS.bonus_every_weeks} weeks, the higher {SETTINGS.bonus_every_weeks}-week return earns a ${SETTINGS.bonus_amount:,.0f} deposit (deposits never count as return).

HOW TO WORK THIS TURN
- Use the research tools as you see fit within your budget (shown in the briefing). Data tools only return data up to the decision date.
- Finish by calling submit_decision exactly once with your final decision. Every slot needs a short reason, and give a one-line reason for each position in position_reasons (shown next to your trades on the public dashboard). If the engine rejects it, fix the listed problems and resubmit.
- Write anything you want to remember in notes_to_self; you'll see it next week."""


def _pct(x) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def build(conn: sqlite3.Connection, agent_id: str, week_no: int, decision_date: str,
          exec_date: str, simulate: bool, budget_calls: int, budget_usd: float) -> str:
    L = SETTINGS.limits
    val = ledger.value_agent(conn, agent_id, decision_date, "close")
    eq = val.equity
    lines = [f"# Week {week_no}: decision date {decision_date}",
             f"Data is available up to the close of {decision_date}. Your orders execute at the open "
             f"of {exec_date}."]

    # Scoreboard ---------------------------------------------------------
    lines += ["", "## Scoreboard",
              "| agent | points | equity | cumulative return | last scored week | this week so far |",
              "|---|---|---|---|---|---|"]
    for row in scoring.scoreboard(conn, decision_date):
        me = " (you)" if row["agent"] == agent_id else ""
        lines.append(f"| {row['agent']}{me} | {row['points']} | ${row['equity']:,.2f} | "
                     f"{_pct(row['cumulative_return'])} | {_pct(row['last_week_return'])} | "
                     f"{_pct(row['week_to_date'])} |")
    n = SETTINGS.bonus_every_weeks
    scored = conn.execute("SELECT COALESCE(MAX(week_no), 0) FROM scores").fetchone()[0]
    nxt = (scored // n + 1) * n
    lines.append(f"Next ${SETTINGS.bonus_amount:,.0f} bonus: after week {nxt} is scored "
                 f"(best chained return over weeks {nxt - n + 1}-{nxt}).")

    # Account ------------------------------------------------------------
    lines += ["", "## Your account",
              f"Equity ${eq:,.2f}; cash ${val.cash:,.2f}; long ${val.long_value:,.2f}; "
              f"short ${val.short_value:,.2f}."]
    if eq > 0:
        lines.append(f"Exposure vs limits: gross {val.gross / eq:.1%} (max {L.max_gross:.0%}), "
                     f"short {val.short_value / eq:.1%} (max {L.max_short_total:.0%}).")
        big = sorted(val.ticker_value.items(), key=lambda kv: -abs(kv[1]))[:5]
        if big:
            lines.append("Largest net tickers: " + ", ".join(f"{t} {v / eq:+.1%}" for t, v in big)
                         + f" (limits: long {L.max_long_per_ticker:.0%}, short {L.max_short_per_ticker:.0%}).")
    if val.stale:
        lines.append(f"Note: no bar on {decision_date} for {val.stale}; valued at their last close.")

    # Slots --------------------------------------------------------------
    lines += ["", "## Your slots"]
    slots = conn.execute("SELECT * FROM slots WHERE agent_id=? AND (active=1 OR slot=0) ORDER BY slot",
                         (agent_id,)).fetchall()
    last_week = conn.execute("SELECT MAX(week_no) FROM slot_scores WHERE agent_id=?", (agent_id,)).fetchone()[0]
    pos_by_slot: dict[int, list] = {}
    for p in ledger.positions(conn, agent_id):
        pos_by_slot.setdefault(p["slot"], []).append(p)
    risk = {(r["slot"], r["ticker"]): r for r in conn.execute(
        "SELECT * FROM risk_orders WHERE agent_id=?", (agent_id,))}
    for s in slots:
        slot = s["slot"]
        s_eq = val.slot_equity.get(slot, 0.0)
        if slot == 0:
            lines.append(f"\n### Cash reserve: ${s_eq:,.2f} ({s_eq / eq:.1%} of equity)" if eq > 0 else "")
            continue
        lw = conn.execute("SELECT weekly_return FROM slot_scores WHERE agent_id=? AND slot=? AND week_no=?",
                          (agent_id, slot, last_week)).fetchone() if last_week else None
        lines.append(f"\n### Slot {slot}: {s['name'] or '(unnamed)'} [{s['type']}]  "
                     f"equity ${s_eq:,.2f} ({s_eq / eq:.1%} of total); last scored week "
                     f"{_pct(lw[0] if lw else None)}; since inception "
                     f"{_pct(scoring.slot_since_inception(conn, agent_id, slot))}")
        rows = pos_by_slot.get(slot, [])
        if rows:
            lines += ["| ticker | shares | avg price | last close | value | % equity | unrealized P&L | stop | take-profit |",
                      "|---|---|---|---|---|---|---|---|---|"]
            for p in rows:
                px = val.prices[p["ticker"]]
                v = p["shares"] * px
                r = risk.get((slot, p["ticker"]))
                lines.append(
                    f"| {p['ticker']} | {p['shares']:.4f} | {p['avg_price']:.2f} | {px:.2f} | {v:,.2f} | "
                    f"{v / eq:+.1%} | {(px - p['avg_price']) * p['shares']:+,.2f} | "
                    f"{r['stop_loss'] if r and r['stop_loss'] else '-'} | "
                    f"{r['take_profit'] if r and r['take_profit'] else '-'} |")
        else:
            lines.append("(no positions)")
        if s["type"] == "code":
            code = latest_code(conn, agent_id, slot) or "(no code saved)"
            lines += ["Current code:", "```python", code[:4000], "```"]

    # Events -------------------------------------------------------------
    since = conn.execute("SELECT MAX(exec_date) FROM weeks WHERE status IN ('executed','scored') "
                         "AND exec_date <= ?", (decision_date,)).fetchone()[0] or "0000"
    events = conn.execute(
        "SELECT date, slot, ticker, shares, fill_price, realized_pnl, reason FROM trades WHERE agent_id=? "
        "AND reason != 'rebalance' AND date >= ? ORDER BY seq", (agent_id, since)).fetchall()
    pending = conn.execute("SELECT * FROM pending_orders WHERE agent_id=? AND status='pending' "
                           "AND kind != 'rebalance' ORDER BY id", (agent_id,)).fetchall()
    failed = conn.execute("SELECT error FROM agent_turns WHERE agent_id=? AND week_no=? AND status='failed'",
                          (agent_id, week_no - 1)).fetchone()
    lines += ["", "## Events since your last trades"]
    if not (events or pending or failed):
        lines.append("None.")
    for t in events:
        lines.append(f"- {t['date']} {t['reason']}: slot {t['slot']} {t['ticker']} {t['shares']:+.4f} sh "
                     f"@ {t['fill_price']:.2f} (realized {t['realized_pnl']:+.2f})")
    for o in pending:
        lines.append(f"- PENDING {o['kind']} for {o['exec_date']} open: slot {o['slot']} {o['ticker']} ({o['note']}). "
                     f"It executes before your new orders.")
    if failed:
        lines.append(f"- Last week your turn FAILED, portfolio was left unchanged: {failed['error']}")

    # Costs --------------------------------------------------------------
    def tot(sql, *params):
        return conn.execute(sql, (agent_id, *params)).fetchone()[0] or 0.0
    lines += ["", "## Costs paid",
              f"Since your last trades: commissions ${tot('SELECT SUM(commission) FROM trades WHERE agent_id=? AND date>=?', since):.2f}, "
              f"slippage ${tot('SELECT SUM(slippage_cost) FROM trades WHERE agent_id=? AND date>=?', since):.2f}, "
              f"borrow ${tot('SELECT SUM(amount) FROM borrow_fees WHERE agent_id=? AND date>=?', since):.2f}.",
              f"All time: commissions ${tot('SELECT SUM(commission) FROM trades WHERE agent_id=?'):.2f}, "
              f"slippage ${tot('SELECT SUM(slippage_cost) FROM trades WHERE agent_id=?'):.2f}, "
              f"borrow ${tot('SELECT SUM(amount) FROM borrow_fees WHERE agent_id=?'):.2f}."]

    # Memory -------------------------------------------------------------
    lines += ["", "## Your last decisions (most recent first)"]
    turns = conn.execute(
        "SELECT t.week_no, t.status, t.decision_json, s.weekly_return, s.point FROM agent_turns t "
        "LEFT JOIN scores s ON s.week_no=t.week_no AND s.agent_id=t.agent_id "
        "WHERE t.agent_id=? AND t.week_no < ? ORDER BY t.week_no DESC LIMIT 4", (agent_id, week_no)).fetchall()
    if not turns:
        lines.append("None yet: this is your first week.")
    for t in turns:
        outcome = (f"return {_pct(t['weekly_return'])}, point {'yes' if t['point'] else 'no'}"
                   if t["weekly_return"] is not None else "not scored yet")
        if t["status"] != "accepted" or not t["decision_json"]:
            lines.append(f"- Week {t['week_no']}: no valid decision ({outcome}).")
            continue
        d = json.loads(t["decision_json"])
        slots_s = "; ".join(f"slot {s.get('slot')} {s.get('name', '')} [{s.get('type')}] {s.get('allocation')}"
                            f"{' ' + json.dumps(s.get('positions')) if s.get('positions') else ''}"
                            for s in d.get("slots", []))
        lines.append(f"- Week {t['week_no']} ({outcome}): view: {str(d.get('market_view', ''))[:500]}\n"
                     f"  slots: {slots_s or 'all cash'}\n  notes_to_self: {str(d.get('notes_to_self', ''))[:1500]}")

    # Strategy reports -------------------------------------------------
    from ..reports import briefing_section
    lines += briefing_section(conn, agent_id, week_no)

    # Universe changes ---------------------------------------------------
    from .. import universe
    info = universe.list_info(conn)
    held_removed = sorted({p["ticker"] for p in ledger.positions(conn, agent_id)} & set(universe.REMOVED))
    lines += ["", "## Universe",
              (f"S&P 500 list dated {info['list_date']}" if info["list_date"] else "The built-in S&P 500 list")
              + f" ({info['members']} stocks) + the ETFs listed in the rules."]
    if held_removed:
        lines.append(f"You hold {', '.join(held_removed)}, which left the S&P 500: you may keep or reduce "
                     f"these positions (same direction, same or smaller weight in the same slot), not add to them.")

    # Budget -------------------------------------------------------------
    lines += ["", "## This turn",
              f"Research budget: {budget_calls} tool calls (web searches count too) and ${budget_usd:.2f} "
              f"of API spend. When it runs out, submit immediately.",
              "Web search: " + (f"available. Soft limit: about {SETTINGS.web_search_soft_limit} searches "
                                f"this turn (the same for both agents; your count is shown on the "
                                f"public dashboard next to your opponent's)." if not simulate else
                                "DISABLED (this is a simulation of a past week; using knowledge of what "
                                "happened after the decision date defeats the purpose)."),
              "When ready, call submit_decision. Example:",
              '{"market_view": "...", "slots": [{"slot": 1, "name": "AI momentum", "type": "manual", '
              '"allocation": 0.4, "positions": {"NVDA": 0.3, "AVGO": 0.3, "XLK": 0.4}, '
              '"risk_orders": {"NVDA": {"stop_loss": 160.0}}, "reason": "...", '
              '"position_reasons": {"NVDA": "...", "AVGO": "...", "XLK": "..."}}, {"slot": 2, '
              '"name": "short weak retail", "type": "code", "allocation": 0.2, "reason": "..."}], '
              '"notes_to_self": "..."}']
    return "\n".join(lines)
