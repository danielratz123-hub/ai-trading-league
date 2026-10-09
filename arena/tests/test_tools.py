"""Phase 2: research tools, backtests, and the no-look-ahead guarantee."""
import json

import numpy as np
import pandas as pd
import pytest

from arena.db import connect
from arena.tools import data_tools, indicators, sandbox_tools
from arena.tools.context import ResearchContext
from arena.tools.data_tools import ToolError
from arena.tools.registry import Budget, ToolBox
from arena.trading_calendar import store_days

TICKERS = ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "XOM", "CVX", "JPM",
           "XLK", "XLE", "XLF", "TLT", "GLD"]
DECISION = "2024-03-28"   # Thursday before Good Friday 2024


def _market(future_multiplier: float = 1.0) -> pd.DataFrame:
    """Deterministic random walks. Bars after DECISION are scaled by
    future_multiplier so tests can detect any leak of future data."""
    days = pd.bdate_range("2022-01-03", "2024-06-28")
    rng = np.random.default_rng(42)
    rows = []
    for i, t in enumerate(TICKERS):
        drift = 0.0004 * ((i % 5) - 2)
        rets = rng.normal(drift, 0.015, len(days))
        close = 100 * np.exp(np.cumsum(rets))
        opens = close * (1 + rng.normal(0, 0.003, len(days)))
        vol = rng.integers(1_000_000, 5_000_000, len(days)).astype(float)
        for d, o, c, v in zip(days, opens, close, vol):
            ds = d.strftime("%Y-%m-%d")
            m = future_multiplier if ds > DECISION else 1.0
            rows.append((t, ds, o * m, max(o, c) * m, min(o, c) * m, c * m, v))
    return pd.DataFrame(rows, columns=["ticker", "date", "open", "high", "low", "close", "volume"])


def _db(future_multiplier: float = 1.0):
    conn = connect(":memory:")
    df = _market(future_multiplier)
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(conn, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    return conn


@pytest.fixture(scope="module")
def ctx():
    c = ResearchContext(_db(), "claude", DECISION)
    yield c
    c.close_session()


@pytest.fixture(scope="module")
def ctx_crazy_future():
    """Same past, absurd future (x50). Anything that differs from `ctx` leaked."""
    c = ResearchContext(_db(future_multiplier=50.0), "claude", DECISION)
    yield c
    c.close_session()


# ------------------------------------------------------------ no look-ahead


def test_context_only_loads_data_up_to_decision_date(ctx):
    assert ctx.frame().index.max() == pd.Timestamp(DECISION)


@pytest.mark.parametrize("call", [
    lambda c: data_tools.get_prices(c, ["AAPL", "SPY"], "2024-01-01", "2024-12-31", ["open", "close"]),
    lambda c: data_tools.get_indicators(c, ["AAPL", "XOM"], ["return_63", "sma_50", "rsi_14", "rs_21", "beta_60"]),
    lambda c: data_tools.get_indicators(c, ["AAPL"], ["vol_20"], {"history_days": 30}),
    lambda c: data_tools.screen(c, ["price > sma_50", "pct_rank(return_63) >= 0.5"], "return_21"),
    lambda c: data_tools.market_overview(c),
])
def test_data_tools_ignore_the_future(ctx, ctx_crazy_future, call):
    assert call(ctx) == call(ctx_crazy_future)


def test_backtest_ignores_the_future(ctx, ctx_crazy_future):
    code = ("def target_weights(prices, date):\n"
            "    r = prices['close'].iloc[-1] / prices['close'].iloc[-21] - 1\n"
            "    best = r.dropna().sort_values().index[-3:]\n"
            "    return {t: 0.3 for t in best}\n")
    a = sandbox_tools.backtest(ctx, "2023-06-01", "2024-12-31", code=code)
    b = sandbox_tools.backtest(ctx_crazy_future, "2023-06-01", "2024-12-31", code=code)
    assert a["ok"] and a == b
    assert a["end"] <= DECISION and a["notes"]


def test_strategy_only_sees_bars_up_to_each_backtest_date(ctx):
    code = ("def target_weights(prices, date):\n"
            "    assert prices.index.max() <= __import__('pandas').Timestamp(date), 'future data!'\n"
            "    return {'SPY': 1.0}\n")
    res = sandbox_tools.backtest(ctx, "2023-01-01", code=code)
    assert res["ok"], res


def test_run_python_cannot_see_past_decision_date(ctx, ctx_crazy_future):
    code = "print(prices.index.max().date(), round(prices['close']['SPY'].iloc[-1], 6))"
    a = sandbox_tools.run_python(ctx, code)
    assert a["ok"] and a["stdout"].startswith(DECISION)
    assert a == sandbox_tools.run_python(ctx_crazy_future, code)


# ------------------------------------------------------------ data tools


def test_get_prices_truncates_and_tells_the_agent(ctx):
    res = data_tools.get_prices(ctx, TICKERS, "2022-01-01", DECISION)
    assert res["rows"] == data_tools.MAX_PRICE_ROWS
    assert "TRUNCATED" in res["notes"][0]
    last_line = res["csv"].strip().splitlines()[-1]
    assert last_line.startswith(DECISION)   # most recent rows are the ones kept


def test_get_prices_rejects_bad_input(ctx):
    with pytest.raises(ToolError, match="Not in the universe"):
        data_tools.get_prices(ctx, ["FAKE"])
    with pytest.raises(ToolError, match="No price data"):
        data_tools.get_prices(ctx, ["AMZN"])   # in universe, but no bars in this test DB


def test_indicator_math():
    close = pd.DataFrame({"SPY": np.arange(1.0, 301.0)})
    assert indicators.compute("sma_3", close)["SPY"].iloc[-1] == pytest.approx(299.0)
    assert indicators.compute("return_10", close)["SPY"].iloc[-1] == pytest.approx(300 / 290 - 1)
    assert indicators.compute("rsi_14", close)["SPY"].iloc[-1] == pytest.approx(100.0)
    assert indicators.compute("drawdown_252", close)["SPY"].iloc[-1] == 0
    with pytest.raises(ValueError, match="Unknown indicator"):
        indicators.parse("magic_10")


def test_screen_filters_and_sorts(ctx):
    res = data_tools.screen(ctx, ["sector == Energy"], sort_by="return_21 asc", universe="sp500")
    assert {r["ticker"] for r in res["results"]} == {"XOM", "CVX"}
    vals = [r["return_21"] for r in res["results"]]
    assert vals == sorted(vals)
    top = data_tools.screen(ctx, ["pct_rank(return_63) >= 0.8"], "return_63")
    assert 1 <= top["matched"] <= 4
    with pytest.raises(ToolError, match="Can't parse"):
        data_tools.screen(ctx, ["return_63 is big"])


def test_market_overview_has_all_sections(ctx):
    m = data_tools.market_overview(ctx)
    assert m["as_of"] == DECISION
    assert {"indexes", "spy_trend", "volatility", "sectors_by_3m_return", "breadth", "macro"} <= set(m)
    assert 0 <= m["breadth"]["pct_above_sma50"] <= 1


# ------------------------------------------------------------ backtest


def test_backtest_fixed_portfolio_tracks_spy_minus_costs(ctx):
    res = sandbox_tools.backtest(ctx, "2023-01-01", "2023-12-29", portfolio={"SPY": 1.0})
    assert res["ok"]
    assert res["total_return"] == pytest.approx(res["spy_return"], abs=0.01)
    assert res["total_return"] < res["spy_return"]          # costs make it slightly worse
    assert res["commissions"] > 0 and res["borrow_fees"] == 0


def test_backtest_short_pays_borrow_and_reports_limit_warnings(ctx):
    res = sandbox_tools.backtest(ctx, "2023-01-01", "2023-06-30", portfolio={"XOM": -0.3, "SPY": 0.7})
    assert res["borrow_fees"] > 0
    assert any("net short 30.0%" in w for w in res["limit_warnings"])


def test_backtest_argument_errors(ctx):
    with pytest.raises(ToolError, match="exactly one"):
        sandbox_tools.backtest(ctx, "2023-01-01")
    with pytest.raises(ToolError, match="at least 4 weeks"):
        sandbox_tools.backtest(ctx, "2024-03-20", portfolio={"SPY": 1})


# ------------------------------------------------------------ code slots


def test_save_strategy_code_validates_output(ctx):
    bad = "def target_weights(prices, date):\n    return {'SPY': 0.8, 'QQQ': -0.5}\n"
    res = sandbox_tools.save_strategy_code(ctx, 2, bad)
    assert not res["saved"] and "sum of |weights|" in res["error"]
    assert sandbox_tools.latest_code(ctx.conn, "claude", 2) is None

    good = "def target_weights(prices, date):\n    return {'SPY': 0.5, 'XOM': -0.2}\n"
    res = sandbox_tools.save_strategy_code(ctx, 2, good)
    assert res["saved"] and res["weights_today"] == {"SPY": 0.5, "XOM": -0.2}
    assert sandbox_tools.run_code_slot(ctx, 2) == ({"SPY": 0.5, "XOM": -0.2}, None)


def test_code_slot_crash_returns_error_not_exception(ctx):
    crash = "def target_weights(prices, date):\n    return 1 / 0\n"
    ctx.conn.execute("INSERT INTO strategy_code(agent_id, slot, saved_date, code) VALUES "
                     "('claude', 3, ?, ?)", (DECISION, crash))
    weights, err = sandbox_tools.run_code_slot(ctx, 3)
    assert weights is None and "ZeroDivisionError" in err
    assert sandbox_tools.run_code_slot(ctx, 4) == (None, "slot 4 is type 'code' but no code was saved for it.")


def test_unknown_ticker_in_strategy_is_rejected(ctx):
    res = sandbox_tools.save_strategy_code(ctx, 1, "def target_weights(p, d):\n    return {'BTC': 0.1}\n")
    assert not res["saved"] and "not in the tradable universe" in res["error"]


# ------------------------------------------------------------ toolbox


def test_toolbox_budget_errors_and_logging(ctx):
    box = ToolBox(ctx, Budget(max_calls=3, max_spend_usd=1.0))
    out = box.call("get_indicators", {"tickers": ["AAPL"], "indicators": ["rsi_14"]})
    assert json.loads(out.rsplit("\n[budget", 1)[0])["values"]["AAPL"]["rsi_14"] is not None
    assert "2 tool calls left" in out
    assert "Not in the universe" in box.call("get_prices", {"tickers": ["NOPE"]})
    assert "Missing required argument" in box.call("get_prices", {})
    assert "budget exhausted" in box.call("market_overview", {}).lower()
    rows = ctx.conn.execute("SELECT tool, ok FROM tool_calls ORDER BY id DESC LIMIT 3").fetchall()
    assert [(r["tool"], r["ok"]) for r in rows] == [("get_prices", 0), ("get_prices", 0),
                                                     ("get_indicators", 1)]


def test_api_spend_also_exhausts_budget(ctx):
    box = ToolBox(ctx, Budget(max_calls=40, max_spend_usd=1.0))
    box.budget.spend_usd = 1.01
    assert "budget exhausted" in box.call("market_overview", {}).lower()


def test_web_search_is_off_in_simulate_mode(ctx):
    assert ToolBox(ctx).web_search_enabled
    sim = ResearchContext(ctx.conn, "claude", DECISION, simulate=True)
    assert not ToolBox(sim).web_search_enabled
    assert "web_search" not in {s["name"] for s in ToolBox(sim).specs()}
