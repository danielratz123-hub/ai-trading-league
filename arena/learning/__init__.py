"""The data, review and learning layer (see LEARNING.md).

    config.py      config/learning.yaml
    schema.py      the tables (SQLite)
    regime.py      market regime classifier (pure)
    collector.py   1-minute bars, daily snapshots, market context (no LLM)
    hardlimits.py  anti-gambling limits, checked before every order
    strategies.py  strategies + versions, cooldowns, rewrite rules
    journal.py     the decisions journal (frozen rows)
    grader.py      outcomes for past decisions (no LLM)
    riskcheck.py   hourly risk rules (no LLM unless one fires)
    review.py      daily / weekly reviews and risk wake-ups (LLM)
    shadow.py      champion vs challenger
    scores.py      risk-adjusted weekly scores
    bootstrap.py   migrates the running league's existing state
"""
