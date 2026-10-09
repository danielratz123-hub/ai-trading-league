"""One agent's research session for one decision date.

Every research tool reads prices through this object, and this object only
ever loads bars dated <= decision_date. That single SQL filter is what
guarantees "no look-ahead" for all tools, code runs and backtests.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pandas as pd

from ..sandbox.runner import PriceDataFile, SandboxLimits
from ..universe import SECTOR_OF

FIELDS = ["open", "high", "low", "close", "volume"]


@dataclass
class ResearchContext:
    conn: sqlite3.Connection
    agent_id: str
    decision_date: str               # last bar the agent may see (Friday's close)
    simulate: bool = False           # True = replaying the past: web search off
    sandbox_limits: SandboxLimits = field(default_factory=SandboxLimits)
    _frame: pd.DataFrame | None = None
    _data_file: PriceDataFile | None = None

    def frame(self) -> pd.DataFrame:
        """All bars up to decision_date. Columns: MultiIndex (field, ticker);
        index: DatetimeIndex. prices['close']['AAPL'] is a price series."""
        if self._frame is None:
            long = pd.read_sql_query(
                "SELECT ticker, date, open, high, low, close, volume FROM prices "
                "WHERE date <= ? ORDER BY date, ticker",
                self.conn, params=(self.decision_date,))
            if long.empty:
                raise RuntimeError(f"No price data on or before {self.decision_date}. "
                                   f"Run update-prices first.")
            long["date"] = pd.to_datetime(long["date"])
            wide = long.pivot(index="date", columns="ticker", values=FIELDS)
            self._frame = wide.sort_index(axis=1)
        return self._frame

    def field(self, name: str) -> pd.DataFrame:
        return self.frame()[name]

    def data_file(self) -> PriceDataFile:
        """Price data for the sandbox, written once per session."""
        if self._data_file is None:
            self._data_file = PriceDataFile(self.frame())
        return self._data_file

    @property
    def universe(self) -> dict[str, str]:
        return dict(SECTOR_OF)

    def close_session(self) -> None:
        if self._data_file is not None:
            self._data_file.cleanup()
            self._data_file = None
