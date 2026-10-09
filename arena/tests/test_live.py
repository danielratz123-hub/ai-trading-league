"""Live dashboard values during market hours (no network)."""
from datetime import datetime

import pandas as pd
import pytest

from arena import ledger
from arena.api import queries
from arena.api.live import LivePrices, market_hours
from arena.db import connect
from arena.ledger import SlotTarget
from arena.prices import ET
from arena.trading_calendar import store_days


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "arena.db"
    conn = connect(path)
    today = datetime.now(ET).date().isoformat()
    store_days(conn, [(today, "09:30", "16:00")])
    for t, p in (("SPY", 500.0), ("AAPL", 100.0), ("XOM", 50.0)):
        conn.execute("INSERT INTO opening_prices(ticker, date, open, source) VALUES (?,?,?,?)", (t, today, p, "t"))
        conn.execute("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", (t, "2000-01-03", p, p, p, p, 1))
    ledger.create_agent(conn, "claude", 1000, today)
    ledger.rebalance(conn, "claude", today, [SlotTarget(1, 1.0, {"AAPL": 0.25, "XOM": 0.2})])
    ledger.snapshot(conn, "claude", today, "post_trade", "open")
    yield path, conn, today
    conn.close()


def test_live_values_use_live_quotes_and_fall_back(db):
    path, conn, today = db
    live = {"enabled": True, "market_open": True, "updated": "14:32", "feed": "iex",
            "quotes": {"AAPL": 110.0, "SPY": 505.0}}                  # no live XOM quote
    snap = queries.live_snapshot(conn, live)
    c = snap["agents"]["claude"]
    aapl = next(p for p in c["positions"] if p["ticker"] == "AAPL")
    xom = next(p for p in c["positions"] if p["ticker"] == "XOM")
    assert aapl["last"] == 110.0 and aapl["unrealized_pnl"] > 0
    assert xom["last"] == 50.0                                        # fell back to today's traded price
    shares = ledger.position_shares(conn, "claude", 1, "AAPL")
    assert c["change_since_last_snapshot"] == pytest.approx(shares * 10 / 999, rel=0.05)
    assert snap["spy_return"] == pytest.approx(505 / 500 - 1)


def test_no_live_values_when_market_closed(db):
    path, conn, today = db
    snap = queries.live_snapshot(conn, {"enabled": True, "market_open": False, "quotes": {"AAPL": 1.0}})
    assert snap["agents"] == {}


class Source:
    feed = "test"

    def __init__(self):
        self.calls = 0

    def fetch_latest(self, tickers, after):
        self.calls += 1
        return pd.DataFrame([(t, 123.0, pd.Timestamp.now(tz="UTC")) for t in tickers], columns=["ticker", "price", "time"])


def test_refresher_only_fetches_during_market_hours(db):
    path, conn, today = db
    src = Source()
    lp = LivePrices(lambda: str(path), source_factory=lambda: src)
    lp.refresh(datetime.fromisoformat(f"{today}T08:00").replace(tzinfo=ET))   # before the open
    assert src.calls == 0 and not lp.market_open
    lp.refresh(datetime.fromisoformat(f"{today}T11:00").replace(tzinfo=ET))
    assert src.calls == 1 and lp.market_open and set(lp.quotes) == {"AAPL", "XOM", "SPY"}
    assert lp.state()["updated"] == "11:00"


def test_market_hours_from_calendar(db):
    path, conn, today = db
    assert market_hours(conn, datetime.fromisoformat(f"{today}T15:59").replace(tzinfo=ET))[0]
    assert not market_hours(conn, datetime.fromisoformat(f"{today}T16:00").replace(tzinfo=ET))[0]
