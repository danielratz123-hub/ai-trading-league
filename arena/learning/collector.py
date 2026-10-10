"""Market data collector (no LLM).

  * every minute in market hours: 1-minute bars (Alpaca, the free IEX feed by
    default: ALPACA_DATA_FEED) for every held symbol, each agent's watchlist,
    SPY, the sector ETFs and the VIX proxy -> bars_1m
  * after the close: daily_snapshots (RSI14, SMA20/50/200, ATR14, 20-day
    average volume) and a market_context row (SPY change, VIX, sector moves,
    headlines, regime)

The minute loop runs as a thread inside the scheduler (scheduler.py) so it
doesn't add 390 job rows a day; the after-close part runs in the
`daily-review` job, before the grader.

Every outside fetch (Alpaca bars, Cboe's VIX file, Alpaca news) is passed in
as a function, so tests run without a network and a failing source only
leaves its own field empty.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import pandas as pd

from .. import ledger
from ..db import transaction
from ..prices import ET, price_matrix
from ..trading_calendar import previous_trading_day, trading_days
from ..tools import indicators
from . import config as lconfig
from .regime import classify

log = logging.getLogger("arena.collector")
VIX = "VIX"          # daily_snapshots symbol for the Cboe VIX close


def _utc(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:00Z")


def close_ts(conn: sqlite3.Connection, d: str) -> str:
    """The UTC timestamp of day d's close (half days included)."""
    row = conn.execute("SELECT close_time FROM calendar WHERE date=?", (d,)).fetchone()
    h, m = map(int, (row[0] if row else "16:00").split(":"))
    return datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=ET).astimezone(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# What to collect
# --------------------------------------------------------------------------

def watched_symbols(conn: sqlite3.Connection) -> list[str]:
    md = lconfig.get().market_data
    syms = {"SPY", md.vix_intraday_proxy, *md.sector_etfs}
    syms |= {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM positions")}
    syms |= {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM watchlists")}
    return sorted(syms)


def set_watchlist(conn: sqlite3.Connection, agent_id: str, tickers: list[str], week_no: int | None) -> list[str]:
    """Replace an agent's watchlist (tradable tickers only, capped)."""
    from ..universe import is_tradable
    cap = lconfig.get().market_data.watchlist_max
    clean = []
    for t in tickers or []:
        t = str(t).upper().strip()
        if is_tradable(t) and t not in clean:
            clean.append(t)
    clean = clean[:cap]
    conn.execute("DELETE FROM watchlists WHERE agent_id=?", (agent_id,))
    conn.executemany("INSERT INTO watchlists(agent_id, ticker, added_week) VALUES (?,?,?)",
                     [(agent_id, t, week_no) for t in clean])
    return clean


# --------------------------------------------------------------------------
# Every minute
# --------------------------------------------------------------------------

MinuteFetch = Callable[[list[str], datetime, datetime], pd.DataFrame]


def store_minute_bars(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for r in df.itertuples(index=False):
        ts = pd.Timestamp(r.timestamp)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        vwap = getattr(r, "vwap", None)
        rows.append((r.symbol, ts.strftime("%Y-%m-%dT%H:%M:00Z"), float(r.open), float(r.high), float(r.low),
                     float(r.close), int(r.volume), None if vwap is None or vwap != vwap else float(vwap)))
    with transaction(conn):
        conn.executemany("INSERT OR REPLACE INTO bars_1m(symbol, ts, open, high, low, close, volume, vwap) "
                         "VALUES (?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def collect_minute(conn: sqlite3.Connection, fetch: MinuteFetch, now: datetime | None = None) -> int:
    """Fetch bars since the last stored one (at most 30 minutes back, so a
    gap after downtime is filled), for all watched symbols."""
    now = now or datetime.now(timezone.utc)
    last = conn.execute("SELECT MAX(ts) FROM bars_1m").fetchone()[0]
    start = now - timedelta(minutes=30)
    if last:
        start = max(start, datetime.fromisoformat(last.replace("Z", "+00:00")) - timedelta(minutes=1))
    return store_minute_bars(conn, fetch(watched_symbols(conn), start, now))


def prune_minute_bars(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    keep = lconfig.get().market_data.minute_bar_retention_days
    cutoff = _utc((now or datetime.now(timezone.utc)) - timedelta(days=keep))
    with transaction(conn):
        return conn.execute("DELETE FROM bars_1m WHERE ts < ?", (cutoff,)).rowcount


def latest_minute_price(conn: sqlite3.Connection, symbol: str, as_of: str | None = None,
                        max_age_minutes: int = 20) -> tuple[float, str] | None:
    """(close of the latest 1-minute bar, its END time) at or before as_of,
    if it is fresh enough; else None."""
    as_of = as_of or datetime.now(timezone.utc).isoformat()
    as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    row = conn.execute("SELECT ts, close FROM bars_1m WHERE symbol=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                       (symbol, _utc(as_of_dt))).fetchone()
    if row is None:
        return None
    end = datetime.fromisoformat(row[0].replace("Z", "+00:00")) + timedelta(minutes=1)
    if as_of_dt - end > timedelta(minutes=max_age_minutes):
        return None
    return float(row[1]), end.isoformat(timespec="seconds")


def prev_close(conn: sqlite3.Connection, symbol: str, d: str) -> float | None:
    """The close before trading day d: the daily bar, else the day's last minute bar / snapshot."""
    p = previous_trading_day(conn, d)
    row = conn.execute("SELECT close FROM prices WHERE ticker=? AND date=?", (symbol, p)).fetchone()
    if row:
        return float(row[0])
    row = conn.execute("SELECT close FROM daily_snapshots WHERE symbol=? AND day=?", (symbol, p)).fetchone()
    if row:
        return float(row[0])
    start, end = _utc(datetime.fromisoformat(p).replace(tzinfo=ET)), _utc(datetime.fromisoformat(d).replace(tzinfo=ET))
    row = conn.execute("SELECT close FROM bars_1m WHERE symbol=? AND ts>=? AND ts<? ORDER BY ts DESC LIMIT 1",
                       (symbol, start, end)).fetchone()
    return float(row[0]) if row else None


# --------------------------------------------------------------------------
# After the close
# --------------------------------------------------------------------------

def atr(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, w: int = 14) -> pd.DataFrame:
    prev = close.shift(1)
    tr = pd.concat([(high - low), (high - prev).abs(), (low - prev).abs()]).groupby(level=0).max()
    return tr.reindex(close.index).ewm(alpha=1 / w, adjust=False, min_periods=w).mean()


def daily_indicators(conn: sqlite3.Connection, symbols: list[str], d: str) -> dict[str, dict]:
    """{symbol: {close, volume, indicators}} for symbols in the prices table."""
    start = (date.fromisoformat(d) - timedelta(days=420)).isoformat()
    close = price_matrix(conn, symbols, start, d, "close")
    if close.empty or d not in close.index:
        return {}
    high = price_matrix(conn, symbols, start, d, "high")
    low = price_matrix(conn, symbols, start, d, "low")
    vol = price_matrix(conn, symbols, start, d, "volume")
    vals = {
        "rsi_14": indicators.compute("rsi_14", close),
        "sma_20": indicators.compute("sma_20", close),
        "sma_50": indicators.compute("sma_50", close),
        "sma_200": indicators.compute("sma_200", close),
        "atr_14": atr(high, low, close),
        "avg_volume_20": vol.rolling(20).mean(),
    }
    out = {}
    for s in close.columns:
        c = close.at[d, s]
        if c != c:          # NaN: no bar that day
            continue
        ind = {k: (None if v.at[d, s] != v.at[d, s] else round(float(v.at[d, s]), 4)) for k, v in vals.items()}
        out[s] = {"close": float(c), "volume": int(vol.at[d, s]) if vol.at[d, s] == vol.at[d, s] else 0,
                  "indicators": ind}
    return out


def fetch_vix_history(url: str | None = None, timeout: int = 20) -> dict[str, float]:
    """{YYYY-MM-DD: VIX close} from Cboe's free daily file."""
    url = url or lconfig.get().market_data.vix_daily_url
    req = urllib.request.Request(url, headers={"User-Agent": "ai-trading-league/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 - fixed https URL from config
        text = r.read().decode("utf-8", errors="replace")
    out = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            m, dd, y = row["DATE"].split("/")
            out[f"{y}-{int(m):02d}-{int(dd):02d}"] = float(row["CLOSE"])
        except (KeyError, ValueError):
            continue
    return out


def fetch_headlines(n: int | None = None) -> list[dict]:
    """Latest market headlines from Alpaca's news API (free with the data keys)."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    from ..config import require_env
    n = n or lconfig.get().market_data.headlines
    client = NewsClient(require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY"))
    news = client.get_news(NewsRequest(symbols="SPY,QQQ,DIA,IWM", limit=n))
    items = news.data.get("news", []) if hasattr(news, "data") else []
    return [{"title": getattr(i, "headline", ""), "source": getattr(i, "source", ""),
             "ts": str(getattr(i, "created_at", ""))} for i in items][:n]


def store_vix(conn: sqlite3.Connection, history: dict[str, float]) -> int:
    rows = [(VIX, d, v, 0, "{}") for d, v in history.items()]
    conn.executemany("INSERT OR REPLACE INTO daily_snapshots(symbol, day, close, volume, indicators) "
                     "VALUES (?,?,?,?,?)", rows)
    return len(rows)


def vix_on_or_before(conn: sqlite3.Connection, d: str) -> tuple[str, float] | None:
    row = conn.execute("SELECT day, close FROM daily_snapshots WHERE symbol=? AND day<=? ORDER BY day DESC LIMIT 1",
                       (VIX, d)).fetchone()
    return (row[0], float(row[1])) if row else None


def context_for_day(conn: sqlite3.Connection, d: str, headlines: list[dict] | None = None) -> dict:
    """The 'close' market_context row for trading day d, from stored prices."""
    cfg = lconfig.get()
    start = (date.fromisoformat(d) - timedelta(days=int(cfg.regime.sma_slow * 1.6) + 30)).isoformat()
    spy = price_matrix(conn, ["SPY"], start, d, "close")
    closes = [] if spy.empty else [float(x) for x in spy["SPY"].dropna().tolist()]
    vix = vix_on_or_before(conn, d)
    vix_val = vix[1] if vix and (date.fromisoformat(d) - date.fromisoformat(vix[0])).days <= 5 else None
    label, inputs = classify(closes, vix_val, cfg.regime)
    if vix:
        inputs["vix_date"] = vix[0]
    spy_change = round((closes[-1] / closes[-2] - 1) * 100, 4) if len(closes) >= 2 else None
    sectors = {}
    p = previous_trading_day(conn, d)
    for etf in cfg.market_data.sector_etfs:
        a = conn.execute("SELECT close FROM prices WHERE ticker=? AND date=?", (etf, p)).fetchone()
        b = conn.execute("SELECT close FROM prices WHERE ticker=? AND date=?", (etf, d)).fetchone()
        if a and b and a[0]:
            sectors[etf] = round((b[0] / a[0] - 1) * 100, 3)
    return {"ts": close_ts(conn, d), "day": d, "kind": "close", "spy_change": spy_change, "vix": vix_val,
            "vix_proxy_change": None, "sector_moves": sectors, "headlines": headlines or [],
            "regime": label, "regime_inputs": inputs}


def write_context(conn: sqlite3.Connection, ctx: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO market_context(ts, day, kind, spy_change, vix, vix_proxy_change, sector_moves, "
        "headlines, regime, regime_inputs) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ctx["ts"], ctx["day"], ctx["kind"], ctx["spy_change"], ctx["vix"], ctx.get("vix_proxy_change"),
         json.dumps(ctx["sector_moves"]), json.dumps(ctx["headlines"]), ctx["regime"],
         json.dumps(ctx["regime_inputs"])))


_DEFAULT = object()     # "use the real source" (looked up at call time, so tests can replace it)


def after_close(conn: sqlite3.Connection, d: str, vix_fetch=_DEFAULT, news_fetch=_DEFAULT,
                extra_daily: Callable[[list[str], str], dict[str, tuple[float, int]]] | None = None) -> dict:
    """Write daily_snapshots + the day's market_context. Needs day d's daily
    bars in `prices` (the `daily` job stores them first). vix_fetch /
    news_fetch: a function, or None to skip that source."""
    vix_fetch = fetch_vix_history if vix_fetch is _DEFAULT else vix_fetch
    news_fetch = fetch_headlines if news_fetch is _DEFAULT else news_fetch
    problems = []
    vix_hist = {}
    if vix_fetch:
        try:
            vix_hist = vix_fetch()
        except Exception as e:  # noqa: BLE001 - VIX is optional; the regime falls back to realized vol
            problems.append(f"VIX: {type(e).__name__}: {e}")
    heads: list[dict] = []
    if news_fetch:
        try:
            heads = news_fetch()
        except Exception as e:  # noqa: BLE001
            problems.append(f"headlines: {type(e).__name__}: {e}")
    syms = watched_symbols(conn)
    snaps = daily_indicators(conn, syms, d)
    extras = {}
    if extra_daily:
        missing = [s for s in syms if s not in snaps]
        try:
            extras = extra_daily(missing, d) if missing else {}
        except Exception as e:  # noqa: BLE001
            problems.append(f"extra daily bars: {type(e).__name__}: {e}")
    with transaction(conn):
        if vix_hist:
            store_vix(conn, {k: v for k, v in vix_hist.items() if k >= (date.fromisoformat(d) - timedelta(days=400)).isoformat()})
        conn.executemany("INSERT OR REPLACE INTO daily_snapshots(symbol, day, close, volume, indicators) VALUES (?,?,?,?,?)",
                         [(s, d, v["close"], v["volume"], json.dumps(v["indicators"])) for s, v in snaps.items()]
                         + [(s, d, c, vol, "{}") for s, (c, vol) in extras.items()])
        ctx = context_for_day(conn, d, heads)
        write_context(conn, ctx)
    return {"day": d, "snapshots": len(snaps) + len(extras), "regime": ctx["regime"], "vix": ctx["vix"],
            "spy_change": ctx["spy_change"], "headlines": len(heads), "problems": problems}


def backfill_context(conn: sqlite3.Connection, end: str, days: int = 60, vix_fetch=_DEFAULT) -> int:
    """Regime history for the last `days` trading days up to `end`, so the
    confirm_days rule works from the first review (bootstrap)."""
    vix_fetch = fetch_vix_history if vix_fetch is _DEFAULT else vix_fetch
    if vix_fetch:
        try:
            hist = vix_fetch()
            with transaction(conn):
                store_vix(conn, {k: v for k, v in hist.items()
                                 if k >= (date.fromisoformat(end) - timedelta(days=days * 2 + 30)).isoformat()})
        except Exception as e:  # noqa: BLE001
            log.warning("VIX history unavailable: %s", e)
    ds = trading_days(conn, (date.fromisoformat(end) - timedelta(days=days * 2)).isoformat(), end)[-days:]
    n = 0
    with transaction(conn):
        for d in ds:
            if conn.execute("SELECT 1 FROM market_context WHERE day=? AND kind='close'", (d,)).fetchone():
                continue
            if conn.execute("SELECT 1 FROM prices WHERE ticker='SPY' AND date=?", (d,)).fetchone() is None:
                continue
            write_context(conn, context_for_day(conn, d))
            n += 1
    return n


def regime_history(conn: sqlite3.Connection, as_of: str, n: int = 30) -> list[tuple[str, str | None]]:
    """(day, regime) of the last n closes at or before as_of, oldest first."""
    rows = conn.execute("SELECT day, regime FROM market_context WHERE kind='close' AND ts<=? "
                        "ORDER BY ts DESC LIMIT ?", (as_of, n)).fetchall()
    return [(r[0], r[1]) for r in reversed(rows)]


# --------------------------------------------------------------------------
# Intraday (risk check)
# --------------------------------------------------------------------------

def intraday_context(conn: sqlite3.Connection, d: str, now: datetime) -> dict:
    """An 'intraday' market_context row from the freshest minute bars."""
    md = lconfig.get().market_data
    as_of = now.astimezone(timezone.utc).isoformat()

    def change(sym):
        last, base = latest_minute_price(conn, sym, as_of), prev_close(conn, sym, d)
        return None if not last or not base else round((last[0] / base - 1) * 100, 4)
    last_close = conn.execute("SELECT regime FROM market_context WHERE kind='close' AND ts<=? ORDER BY ts DESC LIMIT 1",
                              (as_of,)).fetchone()
    return {"ts": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "day": d, "kind": "intraday",
            "spy_change": change("SPY"), "vix": None, "vix_proxy_change": change(md.vix_intraday_proxy),
            "sector_moves": {e: c for e in md.sector_etfs if (c := change(e)) is not None},
            "headlines": [], "regime": last_close[0] if last_close else None, "regime_inputs": {}}


def mark_slots(conn: sqlite3.Connection, agent_id: str, as_of: str, d: str) -> ledger.Valuation:
    """The agent valued at the freshest minute prices (falls back to the last close per ticker)."""
    val = ledger.value_agent(conn, agent_id, previous_trading_day(conn, d), "close")
    prices = dict(val.prices)
    for t in prices:
        p = latest_minute_price(conn, t, as_of)
        if p:
            prices[t] = p[0]
    cash = ledger.slot_cash(conn, agent_id)
    slot_eq = dict(cash)
    tv: dict[str, float] = {}
    long_v = short_v = 0.0
    for p in ledger.positions(conn, agent_id):
        v = p["shares"] * prices[p["ticker"]]
        slot_eq[p["slot"]] = slot_eq.get(p["slot"], 0.0) + v
        tv[p["ticker"]] = tv.get(p["ticker"], 0.0) + v
        long_v, short_v = (long_v + v, short_v) if v >= 0 else (long_v, short_v - v)
    total_cash = sum(cash.values())
    return ledger.Valuation(d, "minute", total_cash + long_v - short_v, total_cash, long_v, short_v, slot_eq, tv, prices)
