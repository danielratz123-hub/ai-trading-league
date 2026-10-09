"""Refreshing the S&P 500 list: added tickers become tradable, removed ones
can be held or reduced but never increased, and nothing is force-sold."""
import pytest

from arena import ledger, risk, universe
from arena.db import connect
from arena.ledger import SlotTarget


@pytest.fixture(autouse=True)
def snapshot_after():
    yield
    universe.load(connect(":memory:"))          # back to the built-in list for other tests


def csv_rows(drop=(), add=()):
    rows = [(t, t + " Inc", s) for t, s in universe._build().items() if s != "ETF" and t not in drop]
    return rows + [(t, t + " Inc", "Industrials") for t in add]


def test_parse_the_source_format():
    text = "Symbol,Security,GICS Sector,GICS Sub-Industry\nBRK.B,Berkshire,Financials,x\nBF-B,Brown-Forman,Consumer Staples,y\n"
    assert universe.parse_csv(text) == [("BRK.B", "Berkshire", "Financials"), ("BF.B", "Brown-Forman", "Consumer Staples")]


def test_refresh_adds_and_removes_and_records_the_date(tmp_path):
    conn = connect(tmp_path / "a.db")
    diff = universe.refresh(conn, csv_rows(drop=("XOM",), add=("NEWCO",)), "2026-10-09", "test.csv", "2026-10-01")
    assert diff == {"added": ["NEWCO"], "removed": ["XOM"], "back": []}
    assert universe.is_tradable("NEWCO") and not universe.is_tradable("XOM") and "XOM" in universe.REMOVED
    assert universe.is_tradable("SPY")                                  # ETFs always stay
    info = universe.list_info(conn)
    assert info["list_date"] == "2026-10-01" and info["source"] == "test.csv"
    # A new connection (another process) sees the same list.
    universe.load(connect(":memory:"))
    assert universe.is_tradable("XOM")
    universe.load(connect(tmp_path / "a.db"))
    assert not universe.is_tradable("XOM")
    # XOM comes back.
    diff = universe.refresh(conn, csv_rows(add=("NEWCO",)), "2026-11-01", "test.csv", None)
    assert diff["back"] == ["XOM"] and universe.is_tradable("XOM")


def test_bad_lists_are_refused(tmp_path):
    conn = connect(tmp_path / "a.db")
    with pytest.raises(ValueError, match="only 3"):
        universe.refresh(conn, csv_rows()[:3], "2026-10-09", "x", None)
    with pytest.raises(ValueError, match="Unknown sectors"):
        universe.refresh(conn, csv_rows() + [("ZZZ", "z", "Space")], "2026-10-09", "x", None)
    assert universe.is_tradable("XOM")          # nothing changed


def test_removed_ticker_can_be_held_or_reduced_never_increased():
    held = {(1, "XOM"): 0.20}
    universe.REMOVED["XOM"] = "Energy"
    universe.SECTOR_OF.pop("XOM")
    ok = lambda w, slot=1: risk.validate_portfolio([SlotTarget(slot, 0.5, {"XOM": w, "SPY": 0.3})],
                                                   reduce_only=held).ok          # noqa: E731
    assert ok(0.20) and ok(0.10) and ok(0.201)      # hold, reduce, price-drift slack
    assert not ok(0.30)                              # increase
    assert not ok(-0.10)                             # flip to short
    assert not ok(0.10, slot=2)                      # into another slot
    res = risk.validate_portfolio([SlotTarget(1, 0.5, {"XOM": 0.3})], reduce_only=held)
    assert "left the S&P 500" in res.errors[0]


def test_removed_tickers_are_not_force_sold_and_keep_their_prices(tmp_path, monkeypatch):
    from arena.tests.test_tools import _market
    from arena.trading_calendar import store_days
    conn = connect(tmp_path / "a.db")
    df = _market()
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(conn, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    ledger.create_agent(conn, "claude", 1000, "2024-01-02")
    ledger.rebalance(conn, "claude", "2024-01-02", [SlotTarget(1, 0.5, {"XOM": 0.3})])
    universe.refresh(conn, csv_rows(drop=("XOM",)), "2024-01-03", "test.csv", None)
    assert ledger.position_shares(conn, "claude", 1, "XOM") > 0       # still held
    # The decision check knows the held weight, and the research tools still show XOM.
    from arena.agents.decision import held_removed_weights
    from arena.tools.context import ResearchContext
    w = held_removed_weights(conn, "claude", "2024-01-05")
    assert w[(1, "XOM")] == pytest.approx(0.3, abs=0.05)
    assert "XOM" in ResearchContext(conn, "claude", "2024-01-05").universe
    assert "XOM" not in ResearchContext(conn, "gpt", "2024-01-05").universe
    # Price updates include it.
    seen = {}
    from arena import main, prices

    class Src:
        feed = "iex"

    monkeypatch.setattr(prices, "AlpacaPriceSource", Src)
    def record(c, s, tickers, on_adjust):
        seen["t"] = tickers
        raise SystemExit      # stop before printing a report

    monkeypatch.setattr(prices, "update_prices", record)
    with pytest.raises(SystemExit):
        main._update_prices(conn, universe.ALL_TICKERS)
    assert "XOM" in seen["t"]
