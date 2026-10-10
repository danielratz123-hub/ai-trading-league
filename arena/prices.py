"""Daily price bars: download from Alpaca, store in the DB, read back.

Prices are fully adjusted (splits AND dividends). That keeps every return
calculation simple (price change = total return), but it means history
changes after every split or dividend: Alpaca rescales all older bars.

How updates handle that:
  1. Download recent bars with a few days of overlap with what we stored.
  2. If the overlap matches, just append the new days.
  3. If the overlap doesn't match, the ticker had a split/dividend. We
     re-download its whole history and tell the ledger the rescale factor,
     so held shares are rescaled and the position's value is unchanged
     (e.g. 2:1 split -> factor 0.5 -> shares x2).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from .config import SETTINGS, require_env
from .db import log_event, transaction

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
BAR_COLUMNS = ["ticker", "date", "open", "high", "low", "close", "volume"]

# Relative difference in overlapping closes above which we treat the ticker
# as adjusted. Small enough to catch a 0.05% dividend, large enough to ignore
# rounding of 4-decimal prices.
ADJUST_TOLERANCE = 1e-4
OVERLAP_DAYS = 10


class PriceSource(Protocol):
    def fetch(self, tickers: list[str], start: str, end: str) -> pd.DataFrame:
        """Return a DataFrame with BAR_COLUMNS for start..end inclusive."""
        ...


# Set the first time Alpaca refuses the SIP feed (free plans): from then on
# this process goes straight to IEX instead of asking (and warning) every time.
_SIP_REFUSED = False


def _sip_refused(exc: Exception, what: str) -> None:
    global _SIP_REFUSED
    if not _SIP_REFUSED:
        log.info("Alpaca SIP %s not available on this plan (%s); using the free IEX feed from now on.",
                 what, type(exc).__name__)
    _SIP_REFUSED = True


class AlpacaPriceSource:
    """Adjusted daily bars from Alpaca. SIP feed, falling back to IEX."""

    BATCH = 100  # symbols per request; the SDK handles pagination

    def __init__(self, feed: str | None = None):
        from alpaca.data.historical import StockHistoricalDataClient

        self.client = StockHistoricalDataClient(
            require_env("ALPACA_API_KEY"), require_env("ALPACA_SECRET_KEY")
        )
        self.feed = (feed or SETTINGS.data_feed).lower()
        if self.feed == "sip" and _SIP_REFUSED:
            self.feed = "iex"

    def _request(self, tickers: list[str], start: str, end: str, feed: str) -> pd.DataFrame:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        req = StockBarsRequest(
            symbol_or_symbols=tickers,
            timeframe=TimeFrame.Day,
            start=datetime.fromisoformat(start).replace(tzinfo=ET),
            end=(datetime.fromisoformat(end) + timedelta(days=1)).replace(tzinfo=ET),
            adjustment=Adjustment.ALL,
            feed=DataFeed(feed),
        )
        return self.client.get_stock_bars(req).df

    def fetch(self, tickers: list[str], start: str, end: str) -> pd.DataFrame:
        frames = []
        for i in range(0, len(tickers), self.BATCH):
            batch = tickers[i:i + self.BATCH]
            try:
                raw = self._request(batch, start, end, self.feed)
            except Exception as exc:  # noqa: BLE001 - SDK raises several types
                if self.feed != "sip":
                    raise
                # Free Alpaca plans can't query recent SIP data.
                _sip_refused(exc, "daily bars")
                self.feed = "iex"
                raw = self._request(batch, start, end, self.feed)
            if raw is not None and not raw.empty:
                frames.append(_normalize_alpaca(raw))
        if not frames:
            return pd.DataFrame(columns=BAR_COLUMNS)
        df = pd.concat(frames, ignore_index=True)
        return _drop_unfinished_today(df)

    def _minute_bars(self, tickers: list[str], start: datetime, end: datetime) -> pd.DataFrame:
        """1-minute bars for [start, end], as columns symbol, timestamp, open, close..."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        frames = []
        for i in range(0, len(tickers), self.BATCH):
            batch = tickers[i:i + self.BATCH]

            def req(feed):
                return self.client.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=batch, timeframe=TimeFrame.Minute, start=start, end=end,
                    adjustment=Adjustment.ALL, feed=DataFeed(feed))).df
            try:
                raw = req(self.feed)
            except Exception as exc:  # noqa: BLE001
                if self.feed != "sip":
                    raise
                # Free plans can't read recent SIP data.
                _sip_refused(exc, "minute bars")
                self.feed = "iex"
                raw = req(self.feed)
            if raw is not None and not raw.empty:
                frames.append(raw.reset_index().sort_values("timestamp"))
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["symbol", "timestamp", "open", "close"])

    def minute_bars(self, tickers: list[str], start: datetime, end: datetime) -> pd.DataFrame:
        """1-minute bars (the collector): symbol, timestamp, open, high, low, close, volume, vwap."""
        return self._minute_bars(tickers, start, end)

    def fetch_opens(self, tickers: list[str], d: str) -> pd.DataFrame:
        """Opening price of each ticker on day d: the open of its first
        1-minute bar between 9:30 and 9:45 ET. Columns: ticker, open."""
        day = datetime.fromisoformat(d)
        bars = self._minute_bars(tickers, day.replace(hour=9, minute=30, tzinfo=ET),
                                 day.replace(hour=9, minute=45, tzinfo=ET))
        if bars.empty:
            return pd.DataFrame(columns=["ticker", "open"])
        first = bars.groupby("symbol", as_index=False).first()
        return first.rename(columns={"symbol": "ticker"})[["ticker", "open"]]

    def fetch_latest(self, tickers: list[str], after: datetime) -> pd.DataFrame:
        """The most recent price of each ticker (close of its last 1-minute
        bar) since `after`. Columns: ticker, price, time."""
        bars = self._minute_bars(tickers, after, datetime.now(ET))
        if bars.empty:
            return pd.DataFrame(columns=["ticker", "price", "time"])
        last = bars.groupby("symbol", as_index=False).last()
        out = last.rename(columns={"symbol": "ticker", "close": "price"})
        # A bar is stamped with its start; its close is the price at the END of that minute.
        out["time"] = pd.to_datetime(out["timestamp"], utc=True) + pd.Timedelta(minutes=1)
        return out[["ticker", "price", "time"]]


def _normalize_alpaca(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.reset_index()
    # Daily bar timestamps are midnight Eastern expressed in UTC.
    df["date"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET).dt.strftime("%Y-%m-%d")
    df = df.rename(columns={"symbol": "ticker"})
    return df[BAR_COLUMNS]


def _drop_unfinished_today(df: pd.DataFrame) -> pd.DataFrame:
    """Today's daily bar is still changing until the close; never store it."""
    now = datetime.now(ET)
    if now.hour < 16 or (now.hour == 16 and now.minute < 30):
        df = df[df["date"] != now.date().isoformat()]
    return df


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

# Called as on_adjust(conn, ticker, factor, date) inside the same transaction
# as the price rewrite, so prices and shares never disagree.
AdjustCallback = Callable[[sqlite3.Connection, str, float, str], None]


@dataclass
class UpdateReport:
    appended: int = 0
    redownloaded: list[str] = field(default_factory=list)
    adjustments: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


def _insert_bars(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    rows = [
        (r.ticker, r.date, float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume))
        for r in df.itertuples(index=False)
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO prices(ticker, date, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def _last_dates(conn: sqlite3.Connection, tickers: Iterable[str]) -> dict[str, str]:
    rows = conn.execute("SELECT ticker, MAX(date) FROM prices GROUP BY ticker").fetchall()
    wanted = set(tickers)
    return {t: d for t, d in rows if t in wanted}


def detect_adjustment(old: pd.Series, new: pd.Series) -> float | None:
    """Compare overlapping closes (indexed by date).

    Returns the rescale factor (new/old) if history was adjusted, else None.
    The factor is measured on the latest overlapping date, because that's the
    price our held positions are currently valued at.
    """
    common = old.index.intersection(new.index)
    if len(common) == 0:
        return None
    last = max(common)
    factor = float(new[last] / old[last])
    ratios = (new[common] / old[common] - 1).abs()
    if abs(factor - 1) > ADJUST_TOLERANCE or ratios.max() > ADJUST_TOLERANCE:
        return factor
    return None


def update_prices(
    conn: sqlite3.Connection,
    source: PriceSource,
    tickers: list[str],
    end: str | None = None,
    history_start: str | None = None,
    on_adjust: AdjustCallback | None = None,
) -> UpdateReport:
    end = end or date.today().isoformat()
    history_start = history_start or SETTINGS.price_history_start
    report = UpdateReport()
    tickers = sorted(set(tickers))
    last = _last_dates(conn, tickers)

    new_tickers = [t for t in tickers if t not in last]
    known = [t for t in tickers if t in last]

    # 1) Brand-new tickers: full history.
    if new_tickers:
        df = source.fetch(new_tickers, history_start, end)
        with transaction(conn):
            report.appended += _insert_bars(conn, df)
        got = set(df["ticker"]) if not df.empty else set()
        report.missing += [t for t in new_tickers if t not in got]

    # 2) Known tickers: fetch a few days of overlap before each ticker's own
    #    last bar. Tickers are grouped by that start date (normally they all
    #    share one), so a single stale or delisted ticker can't force the
    #    whole universe to re-download years of history.
    to_redownload: dict[str, float] = {}
    groups: dict[str, list[str]] = {}
    for t in known:
        start = (date.fromisoformat(last[t]) - timedelta(days=OVERLAP_DAYS)).isoformat()
        groups.setdefault(start, []).append(t)
    for overlap_start, group in sorted(groups.items()):
        recent = source.fetch(group, overlap_start, end)
        for t in group:
            new_t = recent[recent["ticker"] == t]
            if new_t.empty:
                continue
            stored = pd.read_sql_query(
                "SELECT date, close FROM prices WHERE ticker=? AND date>=? ORDER BY date",
                conn, params=(t, overlap_start),
            ).set_index("date")["close"]
            factor = detect_adjustment(stored, new_t.set_index("date")["close"])
            if factor is not None:
                to_redownload[t] = factor
            else:
                with transaction(conn):
                    report.appended += _insert_bars(conn, new_t[new_t["date"] > last[t]])

    # 3) Adjusted tickers: replace history and rescale positions atomically.
    for t, factor in sorted(to_redownload.items()):
        full = source.fetch([t], history_start, end)
        if full.empty:
            log_event(conn, "error", "prices", f"{t}: adjustment detected but re-download empty")
            continue
        with transaction(conn):
            conn.execute("DELETE FROM prices WHERE ticker=?", (t,))
            _insert_bars(conn, full)
            if on_adjust is not None:
                on_adjust(conn, t, factor, last[t])
            log_event(conn, "info", "prices", f"{t}: history re-downloaded, factor {factor:.6f}")
        report.redownloaded.append(t)
        report.adjustments[t] = factor
    return report


def store_opening_prices(conn: sqlite3.Connection, df: pd.DataFrame, d: str, source: str) -> int:
    with transaction(conn):
        conn.executemany(
            "INSERT OR REPLACE INTO opening_prices(ticker, date, open, source) VALUES (?,?,?,?)",
            [(r.ticker, d, float(r.open), source) for r in df.itertuples(index=False)])
    return len(df)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def get_price(conn: sqlite3.Connection, ticker: str, d: str, fld: str = "close") -> float | None:
    """Price on exactly date d (None if not available).

    For 'open', the price the league actually traded at that morning (from
    opening_prices) comes first, so every valuation of that open matches
    the fills; otherwise the daily bar's official open."""
    assert fld in ("open", "high", "low", "close")
    row = None
    if fld == "open":
        row = conn.execute("SELECT open FROM opening_prices WHERE ticker=? AND date=?", (ticker, d)).fetchone()
    if row is None:
        row = conn.execute(f"SELECT {fld} FROM prices WHERE ticker=? AND date=?", (ticker, d)).fetchone()
    return None if row is None else float(row[0])


def price_on_or_before(conn: sqlite3.Connection, ticker: str, d: str,
                       fld: str = "close") -> tuple[str, float] | None:
    """Most recent price at or before d. Used for marking when a bar is missing."""
    assert fld in ("open", "high", "low", "close")
    row = conn.execute(
        f"SELECT date, {fld} FROM prices WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 1",
        (ticker, d),
    ).fetchone()
    return None if row is None else (row[0], float(row[1]))


def get_bars(conn: sqlite3.Connection, tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Long-format bars for tickers in [start, end]. Caller is responsible
    for passing an `end` that is not after the decision date."""
    if not tickers:
        return pd.DataFrame(columns=BAR_COLUMNS)
    marks = ",".join("?" * len(tickers))
    return pd.read_sql_query(
        f"SELECT {', '.join(BAR_COLUMNS)} FROM prices WHERE ticker IN ({marks}) "
        f"AND date BETWEEN ? AND ? ORDER BY date, ticker",
        conn, params=(*tickers, start, end),
    )


def price_matrix(conn: sqlite3.Connection, tickers: list[str], start: str, end: str,
                 fld: str = "close") -> pd.DataFrame:
    """Wide DataFrame: index = date, columns = tickers."""
    bars = get_bars(conn, tickers, start, end)
    if bars.empty:
        return pd.DataFrame()
    return bars.pivot(index="date", columns="ticker", values=fld).sort_index()


def latest_price_date(conn: sqlite3.Connection, ticker: str = "SPY") -> str | None:
    return conn.execute("SELECT MAX(date) FROM prices WHERE ticker=?", (ticker,)).fetchone()[0]
