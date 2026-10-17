"""The backtesting lab: historical data, market-state labels, the backtest engine.

    config.py      config/lab.yaml (human-edited only)
    histdata.py    data/history.db: 2016+ SIP daily bars, nightly append, gap check
    states.py      SPY-only market-state labels for history and live days
    engine.py      event-driven backtest engine (same target_weights interface as code slots)
    walkforward.py walk-forward windows and the locked holdout
    runs.py        backtest_runs log: every run, attempts per idea
    strategies.py  live strategies written as code (for the reproduction check)
    cli.py         python -m arena.lab ...

Nothing here is imported by the live league; it only reads the live
database (and writes its own tables) when a lab command runs.
"""
