import pandas as pd
import pytest

from arena import ledger, prices
from arena.ledger import SlotTarget

from .conftest import DAYS


class FakeSource:
    """Serves bars from an in-memory table; records what was requested."""

    def __init__(self, closes: dict[str, dict[str, float]]):
        self.closes = closes
        self.calls: list[tuple[tuple[str, ...], str, str]] = []

    def fetch(self, tickers, start, end):
        self.calls.append((tuple(tickers), start, end))
        rows = [
            (t, d, c, c, c, c, 1000.0)
            for t in tickers for d, c in self.closes.get(t, {}).items() if start <= d <= end
        ]
        return pd.DataFrame(rows, columns=prices.BAR_COLUMNS)


def test_initial_download_and_incremental_append(conn):
    src = FakeSource({"AAPL": {d: 100.0 for d in DAYS[:10]}})
    rep = prices.update_prices(conn, src, ["AAPL", "NOPE"], end=DAYS[9], history_start=DAYS[0])
    assert rep.appended == 10 and rep.missing == ["NOPE"]

    src.closes["AAPL"].update({d: 101.0 for d in DAYS[10:12]})
    rep = prices.update_prices(conn, src, ["AAPL"], end=DAYS[11], history_start=DAYS[0])
    assert rep.appended == 2 and rep.redownloaded == []
    assert prices.latest_price_date(conn, "AAPL") == DAYS[11]


def test_split_triggers_full_redownload_and_rescales_positions(conn):
    src = FakeSource({"NVDA": {d: 100.0 for d in DAYS[:5]}})
    prices.update_prices(conn, src, ["NVDA"], end=DAYS[4], history_start=DAYS[0])
    ledger.create_agent(conn, "gpt", 1000.0, DAYS[0])
    ledger.rebalance(conn, "gpt", DAYS[4], [SlotTarget(1, 0.2, {"NVDA": 1.0})])
    shares = ledger.position_shares(conn, "gpt", 1, "NVDA")
    value_before = ledger.value_agent(conn, "gpt", DAYS[4]).equity

    # 4-for-1 split on DAYS[5]: Alpaca now reports all older bars / 4.
    src.closes["NVDA"] = {d: 25.0 for d in DAYS[:6]}
    rep = prices.update_prices(conn, src, ["NVDA"], end=DAYS[5], history_start=DAYS[0],
                               on_adjust=ledger.apply_price_adjustment)
    assert rep.redownloaded == ["NVDA"]
    assert rep.adjustments["NVDA"] == pytest.approx(0.25)
    assert prices.get_price(conn, "NVDA", DAYS[0]) == 25.0
    assert ledger.position_shares(conn, "gpt", 1, "NVDA") == pytest.approx(shares * 4)
    assert ledger.value_agent(conn, "gpt", DAYS[5]).equity == pytest.approx(value_before)
    assert conn.execute("SELECT COUNT(*) FROM corporate_actions").fetchone()[0] == 1


def test_dividend_adjustment_credits_longs(conn):
    src = FakeSource({"XOM": {d: 100.0 for d in DAYS[:5]}})
    prices.update_prices(conn, src, ["XOM"], end=DAYS[4], history_start=DAYS[0])
    ledger.create_agent(conn, "a", 1000.0, DAYS[0])
    ledger.rebalance(conn, "a", DAYS[4], [SlotTarget(1, 0.2, {"XOM": 1.0})])
    e0 = ledger.value_agent(conn, "a", DAYS[4]).equity
    # $1 dividend, ex-date DAYS[5]: raw price drops 100 -> 99, old bars scaled by 0.99
    src.closes["XOM"] = {**{d: 99.0 for d in DAYS[:5]}, DAYS[5]: 99.0}
    prices.update_prices(conn, src, ["XOM"], end=DAYS[5], history_start=DAYS[0],
                         on_adjust=ledger.apply_price_adjustment)
    # Price fell by the dividend but the holder's equity didn't: dividend received.
    assert ledger.value_agent(conn, "a", DAYS[5]).equity == pytest.approx(e0)


def test_reads_never_go_past_end_date(conn):
    src = FakeSource({"SPY": {d: float(i) + 1 for i, d in enumerate(DAYS)}})
    prices.update_prices(conn, src, ["SPY"], end=DAYS[-1], history_start=DAYS[0])
    bars = prices.get_bars(conn, ["SPY"], DAYS[0], DAYS[20])
    assert bars["date"].max() == DAYS[20]
    assert prices.price_on_or_before(conn, "SPY", "2024-01-15")[0] == "2024-01-12"  # holiday
