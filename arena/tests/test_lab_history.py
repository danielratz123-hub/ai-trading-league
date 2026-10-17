"""Backtest lab, phase 1: the historical data store (no network)."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from arena.lab import histdata
from arena.prices import BAR_COLUMNS, ET
from arena.scheduler import due_jobs
from arena.tests.conftest import weekdays

CAL = weekdays("2016-01-04", "2016-03-31")


class FakeSource:
    """Bars for every calendar day (price 100 * symbol factor, rising 0.1%/day).
    `fail` = number of calls that raise before answering; `skip` = {symbol: [days]}."""

    def __init__(self, days=CAL, fail: int = 0, skip: dict | None = None, scale: dict | None = None,
                 no_data: set | None = None):
        self.days, self.fail, self.skip = days, fail, skip or {}
        self.scale, self.no_data = scale or {}, no_data or set()
        self.calls: list[tuple[tuple[str, ...], str, str]] = []

    def fetch(self, symbols, start, end):
        self.calls.append((tuple(symbols), start, end))
        if self.fail:
            self.fail -= 1
            raise ConnectionError("rate limited")
        rows = []
        for s in symbols:
            if s in self.no_data:
                continue
            base = 100.0 * self.scale.get(s, 1.0)
            for i, d in enumerate(self.days):
                if start <= d <= end and d not in self.skip.get(s, []):
                    px = base * (1.001 ** i)
                    rows.append((s, d, px, px * 1.01, px * 0.99, px, 2e6))
        return pd.DataFrame(rows, columns=BAR_COLUMNS)

    def calendar(self, start, end):
        return [(d, "09:30", "16:00") for d in self.days if start <= d <= end]


@pytest.fixture
def hconn():
    c = histdata.connect(":memory:")
    yield c
    c.close()


def no_sleep(_):
    pass


def test_download_stores_every_symbol_and_the_survivorship_note(hconn):
    src = FakeSource()
    histdata.store_calendar(hconn, src, "2016-01-01", "2016-12-31")
    rep = histdata.download(hconn, src, ["SPY", "AAPL", "MSFT"], "2016-03-31", sleep=no_sleep)
    assert sorted(rep.done) == ["AAPL", "MSFT", "SPY"] and not rep.failed
    n = hconn.execute("SELECT COUNT(*) FROM hist_bars_daily WHERE symbol='SPY'").fetchone()[0]
    assert n == len(CAL)
    assert "Survivorship bias" in histdata.get_meta(hconn, "survivorship_note")
    assert histdata.gap_check(hconn, "2016-03-31")["ok"]


def test_download_retries_and_resumes_after_a_failed_batch(hconn, monkeypatch):
    from arena.lab import config as labconfig
    cfg = labconfig.get()
    small = cfg.__class__(**{**cfg.__dict__, "history": cfg.history.__class__(
        **{**cfg.history.__dict__, "batch_symbols": 2, "max_retries": 1})})
    monkeypatch.setattr(labconfig, "get", lambda: small)
    src = FakeSource(fail=1)                   # first call fails, retry succeeds
    rep = histdata.download(hconn, src, ["A", "B", "C", "D"], "2016-03-31", sleep=no_sleep)
    assert not rep.failed and len(rep.done) == 4
    assert len(src.calls) == 3                 # 1 failed + 2 batches

    # A batch that keeps failing is marked failed; the next run only retries it.
    c2 = histdata.connect(":memory:")
    src = FakeSource(fail=2)
    rep = histdata.download(c2, src, ["A", "B", "C", "D"], "2016-03-31", sleep=no_sleep)
    assert set(rep.failed) == {"A", "B"} and sorted(rep.done) == ["C", "D"]
    src2 = FakeSource()
    rep2 = histdata.download(c2, src2, ["A", "B", "C", "D"], "2016-03-31", sleep=no_sleep)
    assert rep2.skipped_done == 2 and [c[0] for c in src2.calls] == [("A", "B")]
    assert sorted(rep2.done) == ["A", "B"]


def test_rate_limiter_spaces_calls():
    t = [0.0]
    waits = []
    lim = histdata.RateLimiter(120, sleep=lambda s: (waits.append(s), t.__setitem__(0, t[0] + s)),
                               clock=lambda: t[0])
    lim.wait()
    lim.wait()
    t[0] += 0.2
    lim.wait()
    assert waits == [pytest.approx(0.5), pytest.approx(0.3)]


def test_update_appends_new_days_and_redownloads_adjusted_symbols(hconn):
    first = [d for d in CAL if d <= "2016-02-29"]
    histdata.store_calendar(hconn, FakeSource(), "2016-01-01", "2016-12-31")
    histdata.download(hconn, FakeSource(days=first), ["SPY", "AAPL"], "2016-02-29", sleep=no_sleep)
    # AAPL "split 2:1" in March: the whole adjusted history is rescaled.
    rep = histdata.update(hconn, FakeSource(scale={"AAPL": 0.5}), ["SPY", "AAPL"], "2016-03-31", sleep=no_sleep)
    assert set(rep.redownloaded) == {"AAPL"} and rep.redownloaded["AAPL"] == pytest.approx(0.5)
    spy = hconn.execute("SELECT COUNT(*), MAX(day) FROM hist_bars_daily WHERE symbol='SPY'").fetchone()
    assert tuple(spy) == (len(CAL), "2016-03-31")
    aapl_first = hconn.execute("SELECT close FROM hist_bars_daily WHERE symbol='AAPL' ORDER BY day LIMIT 1").fetchone()[0]
    assert aapl_first == pytest.approx(50.0)
    assert histdata.gap_check(hconn, "2016-03-31")["ok"]


def test_gap_check_reports_missing_stale_and_listings(hconn):
    src = FakeSource(skip={"MSFT": ["2016-02-10"], "NEWCO": [d for d in CAL if d < "2016-02-01"],
                           "GONE": [d for d in CAL if d > "2016-03-01"]}, no_data={"NADA"})
    histdata.store_calendar(hconn, src, "2016-01-01", "2016-12-31")
    histdata.download(hconn, src, ["SPY", "MSFT", "NEWCO", "GONE", "NADA"], "2016-03-31", sleep=no_sleep)
    rep = histdata.gap_check(hconn, "2016-03-31")
    assert not rep["ok"]
    assert rep["missing"] == {"MSFT": ["2016-02-10"]}
    assert rep["late_start"] == {"NEWCO": "2016-02-01"}      # a listing, not a gap
    assert rep["stale"] == {"GONE": "2016-03-01"}
    assert rep["no_data"] == ["NADA"]
    assert rep["spy_first_day"] == CAL[0]


def test_bars_frame_matches_the_live_research_layout(hconn):
    src = FakeSource()
    histdata.download(hconn, src, ["SPY", "AAPL"], "2016-03-31", sleep=no_sleep)
    f = histdata.bars(hconn, end="2016-01-29")
    assert list(f.columns.levels[0]) == ["close", "high", "low", "open", "volume"]
    assert f.index.max() == pd.Timestamp("2016-01-29")
    assert f["close"]["SPY"].iloc[0] == pytest.approx(100.0)


def test_nightly_history_job_only_when_enabled():
    cal = {"2026-10-16": "16:00"}
    at_8pm = datetime(2026, 10, 16, 20, 0, tzinfo=ET)
    assert not any(j.name == "history-update" for j in due_jobs(at_8pm, cal, {}, True, None))
    jobs = due_jobs(at_8pm, cal, {}, True, None, history_at=(19, 0))
    assert ("history-update", "history-2026-10-16") in [(j.name, j.key) for j in jobs]
    early = datetime(2026, 10, 16, 18, 0, tzinfo=ET)
    assert not any(j.name == "history-update" for j in due_jobs(early, cal, {}, True, None, history_at=(19, 0)))
