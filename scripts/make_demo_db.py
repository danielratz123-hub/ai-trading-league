"""Build data/demo.db: a few simulated weeks with scripted (fake) agents on
synthetic prices, so you can look at the dashboard without real data or
any AI usage.   python scripts/make_demo_db.py   then   python -m arena.main serve --db data/demo.db
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arena import league  # noqa: E402
from arena.db import connect  # noqa: E402
from arena.tests.fakes import Factory, manual, submit  # noqa: E402
from arena.tests.test_tools import _market  # noqa: E402
from arena.trading_calendar import store_days  # noqa: E402

CODE = ("def target_weights(prices, date):\n"
        "    # 3-month momentum: long the 3 strongest, short the weakest\n"
        "    c = prices['close'].iloc[-64:]\n"
        "    r = (c.iloc[-1] / c.iloc[0] - 1).dropna().drop(['SPY', 'TLT', 'IEF', 'GLD'], errors='ignore')\n"
        "    w = {t: 0.25 for t in r.nlargest(3).index}\n"
        "    w[r.idxmin()] = -0.2\n"
        "    return w\n")


def main() -> None:
    path = Path("data/demo.db")
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        path.unlink()
    conn = connect(path)
    df = _market()
    conn.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?)", df.itertuples(index=False))
    store_days(conn, [(d, "09:30", "16:00") for d in sorted(df["date"].unique())])
    claude_week = [
        [("market_overview", {})],
        [("save_strategy_code", {"slot": 2, "code": CODE})],
        [submit([manual(1, 0.5, {"AAPL": 0.4, "MSFT": 0.3, "XLK": 0.3}, name="Mega-cap tech",
                        risk_orders={"AAPL": {"stop_loss": 50.0}}),
                 {"slot": 2, "name": "Momentum algo", "type": "code", "allocation": 0.4,
                  "reason": "Systematic 3-month momentum with a short hedge."}],
                view="Breadth is narrow; leadership in tech. Staying long quality with a momentum sleeve.",
                notes="Watch XLK vs SPY relative strength.")],
    ]
    gpt_week = [
        [("screen", {"conditions": ["price > sma_50"], "sort_by": "return_21", "limit": 5})],
        [submit([manual(1, 0.6, {"SPY": 0.4, "XOM": 0.3, "CVX": 0.3}, name="Value + energy"),
                 manual(2, 0.3, {"TLT": 0.6, "GLD": 0.4}, name="Defensive"),
                 manual(3, 0.1, {"NVDA": -1.0}, name="Short froth")],
                view="Rates look stable; energy has relative strength. Hedging with bonds and gold.",
                notes="Consider cutting the NVDA short if it breaks out.")],
    ]
    f = Factory({"claude": [claude_week], "gpt": [gpt_week]})
    league.simulate(conn, "2024-01-02", 6, factory=f, progress=print)
    # One more decided-but-unscored week, to show the "revealed after scoring" state.
    league.decide(conn, league.simulation_dates(conn, "2024-01-02", 8)[7], simulate=True, factory=f)
    conn.close()
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
