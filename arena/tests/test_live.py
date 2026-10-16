"""Live dashboard values during market hours (no network)."""
from datetime import datetime

import pytest

from arena import ledger
from arena.api import queries
from arena.api.live import market_hours
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


def test_market_hours_from_calendar(db):
    path, conn, today = db
    assert market_hours(conn, datetime.fromisoformat(f"{today}T15:59").replace(tzinfo=ET))[0]
    assert not market_hours(conn, datetime.fromisoformat(f"{today}T16:00").replace(tzinfo=ET))[0]


# ---------------------------------------------------------------- the Alpaca stream (fake server)

import asyncio
import json
import threading

from arena.api import live as live_mod


class FakeAlpaca:
    """Speaks Alpaca's market-data stream protocol on localhost."""

    def __init__(self, error_on_auth=None):
        self.subscribed: set[str] = set()
        self.connections = 0
        self.error_on_auth = error_on_auth
        self.clients = []
        self.loop = None
        self.port = None
        ready = threading.Event()

        async def handler(ws):
            self.connections += 1
            self.clients.append(ws)
            await ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
            auth = json.loads(await ws.recv())
            assert auth["action"] == "auth" and auth["key"] == "k" and auth["secret"] == "s"
            if self.error_on_auth:
                await ws.send(json.dumps([{"T": "error", "code": self.error_on_auth,
                                           "msg": "connection limit exceeded"}]))
                return
            await ws.send(json.dumps([{"T": "success", "msg": "authenticated"}]))
            async for raw in ws:
                m = json.loads(raw)
                if m["action"] == "subscribe":
                    self.subscribed |= set(m["trades"])
                elif m["action"] == "unsubscribe":
                    self.subscribed -= set(m["trades"])
                await ws.send(json.dumps([{"T": "subscription", "trades": sorted(self.subscribed)}]))

        def run():
            from websockets.asyncio.server import serve

            async def main():
                self.loop = asyncio.get_running_loop()
                async with serve(handler, "127.0.0.1", 0) as server:
                    self.port = server.sockets[0].getsockname()[1]
                    ready.set()
                    await asyncio.Future()
            asyncio.run(main())

        threading.Thread(target=run, daemon=True).start()
        ready.wait(10)

    def trade(self, symbol, price, ts=None):
        # today, after the REST seed (14:00 ET), so the trade is the newest quote
        ts = ts or f"{datetime.now(ET).date().isoformat()}T22:30:01.123456789Z"
        msg = json.dumps([{"T": "t", "S": symbol, "p": price, "s": 5, "t": ts}])
        for ws in list(self.clients):
            asyncio.run_coroutine_threadsafe(ws.send(msg), self.loop)

    def drop(self):
        for ws in list(self.clients):
            asyncio.run_coroutine_threadsafe(ws.close(), self.loop)
        self.clients.clear()


def _et_hhmm(iso):
    return datetime.fromisoformat(iso).astimezone(ET).strftime("%H:%M")


def wait_for(cond, timeout=10.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def stream_env(db, monkeypatch):
    path, conn, today = db
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setattr(live_mod, "REPLAN_SECONDS", 0.5)
    monkeypatch.setattr(live_mod, "FLUSH_SECONDS", 0.1)
    monkeypatch.setattr(live_mod, "MAX_BACKOFF", 0.5)
    rest_calls = []

    def rest(symbols):
        rest_calls.append(list(symbols))
        return {t: (100.0, f"{today}T14:00:00-04:00") for t in symbols}

    def open_clock():
        return datetime.fromisoformat(f"{today}T11:00").replace(tzinfo=ET)
    return path, conn, today, rest, rest_calls, open_clock


def test_stream_subscribes_holdings_and_spy_and_pushes_prices(stream_env):
    path, conn, today, rest, rest_calls, clock = stream_env
    fake = FakeAlpaca()
    ls = live_mod.LiveStream(lambda: str(path), feed="iex", url=f"ws://127.0.0.1:{fake.port}",
                             rest_latest=rest, clock=clock)
    loop = asyncio.new_event_loop()
    q = asyncio.Queue()
    ls.hub.add(loop, q)
    ls.start()
    try:
        assert wait_for(lambda: ls.status == "live")
        assert fake.subscribed == {"AAPL", "XOM", "SPY"} and fake.connections == 1
        assert rest_calls and set(rest_calls[0]) == {"AAPL", "XOM", "SPY"}       # an instant price for all
        fake.trade("AAPL", 111.5)
        assert wait_for(lambda: ls.quotes.get("AAPL") == 111.5)
        assert ls.state()["label"] == "Live · IEX" and ls.state()["updated"] == _et_hhmm(f"{today}T22:30:00+00:00")
        loop.run_until_complete(asyncio.sleep(0.3))
        received = []
        while not q.empty():
            received.append(q.get_nowait())
        assert any(e["type"] == "prices" and e["quotes"].get("AAPL") == 111.5 for e in received)
        # A new position (after an execute) is picked up by the next replan.
        conn.execute("INSERT INTO prices VALUES ('MSFT', '2000-01-03', 300, 300, 300, 300, 1)")
        conn.execute("INSERT INTO opening_prices(ticker, date, open, source) VALUES ('MSFT', ?, 300, 't')", (today,))
        ledger.rebalance(conn, "claude", today, [SlotTarget(1, 1.0, {"AAPL": 0.25, "XOM": 0.2, "MSFT": 0.1})])
        assert wait_for(lambda: "MSFT" in fake.subscribed)
        # The stream drops: it reconnects by itself.
        fake.drop()
        assert wait_for(lambda: fake.connections == 2 and ls.status == "live", timeout=15)
    finally:
        ls.stop()
        loop.close()


def test_no_connection_outside_market_hours(stream_env):
    path, conn, today, rest, rest_calls, _ = stream_env
    fake = FakeAlpaca()

    def night():
        return datetime.fromisoformat(f"{today}T20:00").replace(tzinfo=ET)
    ls = live_mod.LiveStream(lambda: str(path), feed="iex", url=f"ws://127.0.0.1:{fake.port}",
                             rest_latest=rest, clock=night)
    ls.start()
    try:
        assert wait_for(lambda: ls.status == "closed")
        assert fake.connections == 0 and not ls.market_open
    finally:
        ls.stop()


def test_connection_limit_error_backs_off_and_reports(stream_env):
    path, conn, today, rest, rest_calls, clock = stream_env
    fake = FakeAlpaca(error_on_auth=406)
    ls = live_mod.LiveStream(lambda: str(path), feed="iex", url=f"ws://127.0.0.1:{fake.port}",
                             rest_latest=rest, clock=clock)
    ls.start()
    try:
        assert wait_for(lambda: ls.status == "reconnecting" and "406" in (ls.error or ""))
        assert wait_for(lambda: fake.connections >= 2)          # retries, with backoff
    finally:
        ls.stop()


def test_more_than_30_tickers_stream_the_largest_and_poll_the_rest():
    holdings = {f"T{i:02d}": float(i) for i in range(1, 41)}   # T40 is the largest
    holdings["SPY"] = 0.0
    streamed, polled = live_mod.plan_symbols(holdings)
    assert len(streamed) == 30 and "SPY" in streamed and "T40" in streamed and "T12" in streamed
    assert sorted(polled) == [f"T{i:02d}" for i in range(1, 12)]       # the 11 smallest (SPY takes a place)


def test_alpaca_nanosecond_times_are_understood():
    d = live_mod._parse("2026-10-12T14:30:01.123456789Z")
    assert d is not None and d.strftime("%H:%M:%S") == "10:30:01"


def test_sse_stream_sends_snapshot_then_prices_and_ends_when_the_browser_leaves():
    class Src:
        def state(self):
            return {"status": "live", "label": "Live · IEX", "quotes": {"AAPL": 101.0}}

    async def run():
        q = asyncio.Queue()
        left = {"v": False}

        async def gone():
            return left["v"]
        gen = live_mod.sse_events(Src(), q, gone, ping=0.05)
        first = await gen.__anext__()
        assert first.startswith("event: snapshot") and '"AAPL": 101.0' in first
        assert "secret" not in first.lower()
        assert await gen.__anext__() == ": keep-alive\n\n"          # idle: keep-alive comment
        q.put_nowait({"type": "prices", "quotes": {"AAPL": 102.5}})
        ev = await gen.__anext__()
        assert ev.startswith("event: prices") and "102.5" in ev
        left["v"] = True
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
    asyncio.run(run())


def test_stream_endpoint_needs_login(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import arena.api.app as app_module
    from arena.api import auth
    connect(tmp_path / "a.db").close()
    monkeypatch.setenv("ARENA_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("DASHBOARD_PASSWORD_HASH", auth.hash_password("correct horse battery"))
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    with TestClient(app_module.create_app(), base_url="https://testserver") as c:
        assert c.get("/api/stream").status_code == 401
