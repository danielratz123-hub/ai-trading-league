"""Phase 5: the optional Alpaca paper-account mirror (with a fake broker)."""
import pytest

from arena import ledger, mirror
from arena.ledger import SlotTarget

from .conftest import DAYS, flat_prices


class FakeBroker:
    def __init__(self, equity, positions=None, reject=()):
        self._equity, self._pos, self.reject, self.orders = equity, dict(positions or {}), set(reject), []

    def equity(self):
        return self._equity

    def positions(self):
        return dict(self._pos)

    def submit(self, ticker, qty, label=""):
        if ticker in self.reject:
            raise RuntimeError("asset not shortable")
        self.orders.append((ticker, qty))
        self.labels = getattr(self, "labels", []) + [(ticker, label)]
        self._pos[ticker] = self._pos.get(ticker, 0.0) + qty
        return f"order-{len(self.orders)}"


def test_plan_scales_weights_to_the_paper_account():
    lines = {l.ticker: l for l in mirror.plan({"AAPL": 0.25, "SPY": 0.5}, {"AAPL": 200.0, "SPY": 500.0},
                                              100_000, {})}
    assert lines["AAPL"].orders == [pytest.approx(125.0)]       # 25% of $100k / $200
    assert lines["SPY"].orders == [pytest.approx(100.0)]


def test_fractional_longs_but_whole_share_shorts():
    lines = {l.ticker: l for l in mirror.plan({"AAPL": 0.001, "XOM": -0.0037, "TSLA": -0.0005},
                                              {"AAPL": 200.0, "XOM": 100.0, "TSLA": 250.0}, 100_000, {})}
    assert lines["AAPL"].orders == [pytest.approx(0.5)]          # fractional long is fine
    assert lines["XOM"].target_qty == -3 and "rounded" in lines["XOM"].note        # -3.7 -> -3
    assert lines["TSLA"].orders == [] and "under 1 share" in lines["TSLA"].note    # -0.2 -> skipped


def test_flip_long_to_short_uses_two_orders():
    (line,) = mirror.plan({"NVDA": -0.05}, {"NVDA": 100.0}, 10_000, {"NVDA": 3.5})
    assert line.orders == [-3.5, -5] and "two orders" in line.note


def test_positions_the_leader_no_longer_holds_are_closed_and_tiny_changes_skipped():
    lines = {l.ticker: l for l in mirror.plan({"SPY": 0.5}, {"SPY": 500.0, "GLD": 200.0}, 10_000,
                                              {"GLD": 4.0, "SPY": 10.001})}
    assert lines["GLD"].orders == [-4.0]
    assert lines["SPY"].orders == []         # $0.50 difference: below the $1 minimum


def test_leader_needs_a_clear_lead(conn):
    for a in ("claude", "gpt"):
        ledger.create_agent(conn, a, 1000, DAYS[0])
    assert mirror.leader(conn) is None                         # tied at zero
    conn.execute("INSERT INTO scores VALUES (1, 'gpt', 0.02, 1, 1000, 1020)")
    conn.execute("INSERT INTO scores VALUES (1, 'claude', 0.01, 0, 1000, 1010)")
    assert mirror.leader(conn) == "gpt"


def test_sync_places_orders_and_logs_differences(conn):
    flat_prices(conn, "AAPL", 100)
    flat_prices(conn, "XOM", 50)
    for a in ("claude", "gpt"):
        ledger.create_agent(conn, a, 1000, DAYS[0])
    ledger.rebalance(conn, "gpt", DAYS[1], [SlotTarget(1, 1.0, {"AAPL": 0.25, "XOM": -0.1})])
    conn.execute("INSERT INTO scores VALUES (1, 'gpt', 0.02, 1, 1000, 1020)")
    conn.execute("INSERT INTO scores VALUES (1, 'claude', 0.01, 0, 1000, 1010)")
    broker = FakeBroker(10_000)
    mirror.sync(conn, DAYS[2], broker)
    orders = dict(broker.orders)
    assert orders["AAPL"] == pytest.approx(25 * 0.999, rel=0.01)     # ~25% of $10k at $100
    assert orders["XOM"] == -20                                         # ~-20.01 sh -> -20 whole shares
    assert [t for t, _ in broker.orders] == ["XOM", "AAPL"]            # short (cash-raising) first
    rows = {r["ticker"]: r for r in conn.execute("SELECT * FROM mirror_log")}
    assert rows["XOM"]["leader"] == "gpt" and "rounded" in rows["XOM"]["note"]


def test_rejected_order_is_logged_not_fatal(conn):
    flat_prices(conn, "AAPL", 100)
    flat_prices(conn, "XOM", 50)
    ledger.create_agent(conn, "gpt", 1000, DAYS[0])
    ledger.rebalance(conn, "gpt", DAYS[1], [SlotTarget(1, 1.0, {"AAPL": 0.25, "XOM": -0.1})])
    broker = FakeBroker(10_000, reject={"XOM"})
    lines = {l.ticker: l for l in mirror.sync(conn, DAYS[2], broker)}
    assert "rejected" in lines["XOM"].note and ("AAPL", pytest.approx(lines["AAPL"].orders[0])) in broker.orders


def test_dry_run_places_nothing(conn):
    flat_prices(conn, "AAPL", 100)
    ledger.create_agent(conn, "gpt", 1000, DAYS[0])
    ledger.rebalance(conn, "gpt", DAYS[1], [SlotTarget(1, 1.0, {"AAPL": 0.25})])
    broker = FakeBroker(10_000)
    lines = mirror.sync(conn, DAYS[2], broker, dry_run=True)
    assert lines and broker.orders == []
    assert conn.execute("SELECT COUNT(*) FROM mirror_log").fetchone()[0] == 0


# ---------------------------------------------------------------- one paper account per agent


def two_agents_trading(conn):
    flat_prices(conn, "AAPL", 100)
    flat_prices(conn, "XOM", 50)
    for a in ("claude", "gpt"):
        ledger.create_agent(conn, a, 1000, DAYS[0])
    ledger.rebalance(conn, "claude", DAYS[1], [SlotTarget(1, 1.0, {"AAPL": 0.25})])
    ledger.rebalance(conn, "gpt", DAYS[1], [SlotTarget(1, 1.0, {"XOM": 0.2})])


def test_each_mode_mirrors_every_agent_into_its_own_account(conn, monkeypatch):
    two_agents_trading(conn)
    monkeypatch.setenv("MIRROR_MODE", "each")
    brokers = {"claude": FakeBroker(100_000), "gpt": FakeBroker(50_000)}
    results = mirror.sync_all(conn, DAYS[2], brokers=brokers)
    assert set(results) == {"claude", "gpt"}                       # no leader needed
    assert [t for t, _ in brokers["claude"].orders] == ["AAPL"]     # each account gets only its own AI
    assert [t for t, _ in brokers["gpt"].orders] == ["XOM"]
    assert dict(brokers["gpt"].orders)["XOM"] == pytest.approx(0.2 * 50_000 / 50, rel=0.01)


def test_two_agents_cannot_share_one_paper_account(monkeypatch):
    class SameAccount:
        def __init__(self, *a):
            pass

        def account_id(self):
            return "PA123"
    monkeypatch.setattr(mirror, "AlpacaPaperBroker", SameAccount)
    monkeypatch.setattr(mirror, "keys_for", lambda a: ("k", "s"))
    with pytest.raises(ValueError, match="same Alpaca paper account"):
        mirror.brokers_for_each(["claude", "gpt"])


def test_mirror_mode_and_keys(monkeypatch):
    monkeypatch.delenv("MIRROR_MODE", raising=False)
    monkeypatch.setenv("MIRROR_PAPER", "1")
    assert mirror.mode() == "leader"                                 # older setting still works
    monkeypatch.setenv("MIRROR_MODE", "off")
    assert mirror.mode() == "off"
    monkeypatch.setenv("ALPACA_API_KEY", "main-k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "main-s")
    monkeypatch.delenv("ALPACA_MIRROR_CLAUDE_KEY", raising=False)
    monkeypatch.delenv("ALPACA_MIRROR_GPT_KEY", raising=False)
    assert mirror.keys_for("claude") == ("main-k", "main-s")          # Claude defaults to the main account
    with pytest.raises(Exception, match="Setup, step 6"):
        mirror.keys_for("gpt")                                       # ChatGPT needs its own


def test_each_mode_copies_claude_even_before_chatgpt_has_an_account(conn, monkeypatch):
    two_agents_trading(conn)
    monkeypatch.setenv("MIRROR_MODE", "each")

    class Broker(FakeBroker):
        def account_id(self):
            return "PA-main"
    made = {}

    def fake_broker(key, secret):
        made[key] = Broker(100_000)
        return made[key]

    def fake_keys(agent):
        if agent == "gpt":
            raise mirror.MissingKeyError("no gpt keys")
        return ("main-key", "s")
    monkeypatch.setattr(mirror, "AlpacaPaperBroker", fake_broker)
    monkeypatch.setattr(mirror, "keys_for", fake_keys)
    results = mirror.sync_all(conn, DAYS[2])
    assert list(results) == ["claude"] and [t for t, _ in made["main-key"].orders] == ["AAPL"]



# ---------------------------------------------------------------- both agents in one account


def agents_with(conn, claude_w, gpt_w):
    flat_prices(conn, "AAPL", 100)
    flat_prices(conn, "XOM", 50)
    for a in ("claude", "gpt"):
        ledger.create_agent(conn, a, 1000, DAYS[0])
    ledger.rebalance(conn, "claude", DAYS[1], [SlotTarget(1, 1.0, claude_w)])
    ledger.rebalance(conn, "gpt", DAYS[1], [SlotTarget(1, 1.0, gpt_w)])


def test_combined_splits_one_account_and_labels_orders(conn, monkeypatch):
    agents_with(conn, {"AAPL": 0.25}, {"XOM": 0.2})
    monkeypatch.setenv("MIRROR_MODE", "combined")
    b = FakeBroker(100_000)
    mirror.sync_all(conn, DAYS[2], brokers={"combined": b})
    orders = dict(b.orders)
    assert orders["AAPL"] == pytest.approx(0.25 * 50_000 / 100, rel=0.01)    # half the account each
    assert orders["XOM"] == pytest.approx(0.2 * 50_000 / 50, rel=0.01)
    assert dict(b.labels) == {"AAPL": "claude", "XOM": "gpt"}
    held = {(r["agent_id"], r["ticker"]): r["qty"] for r in conn.execute("SELECT * FROM mirror_holdings")}
    assert set(held) == {("claude", "AAPL"), ("gpt", "XOM")}


def test_combined_sends_one_net_order_when_both_trade_a_ticker(conn, monkeypatch):
    agents_with(conn, {"AAPL": 0.25}, {"AAPL": -0.1})       # opposite bets on AAPL
    monkeypatch.setenv("MIRROR_MODE", "combined")
    b = FakeBroker(100_000)
    lines = mirror.sync_all(conn, DAYS[2], brokers={"combined": b})
    assert [t for t, _ in b.orders] == ["AAPL"]              # ONE order, not two fighting ones
    assert b.orders[0][1] == pytest.approx((0.25 - 0.1) * 50_000 / 100, rel=0.02)
    assert dict(b.labels)["AAPL"] == "claude+gpt"
    assert any("shared order" in l.note for l in lines["gpt"])
    held = {(r["agent_id"], r["ticker"]): r["qty"] for r in conn.execute("SELECT * FROM mirror_holdings")}
    assert held[("claude", "AAPL")] > 0 > held[("gpt", "AAPL")]  # each agent's part is still recorded
    # Nothing changed -> nothing sent the next time.
    b2 = FakeBroker(100_000, positions=b.positions())
    mirror.sync_all(conn, DAYS[2], brokers={"combined": b2})
    assert b2.orders == []


def test_dashboard_shows_whose_shares_are_whose(conn, monkeypatch):
    from arena.api import queries
    agents_with(conn, {"AAPL": 0.25}, {"AAPL": 0.1, "XOM": 0.2})
    monkeypatch.setenv("MIRROR_MODE", "combined")
    mirror.sync_all(conn, DAYS[2], brokers={"combined": FakeBroker(100_000)})
    m = queries.mirror_overview(conn)
    aapl = next(h for h in m["holdings"] if h["ticker"] == "AAPL")
    assert set(aapl["by_agent"]) == {"claude", "gpt"} and aapl["total"] > aapl["by_agent"]["claude"]
    assert {o["agent"] for o in m["orders"]} == {"claude", "gpt"} and m["last_sync"]["mode"] == "combined"


# ---------------------------------------------------------------- exact sizing (MIRROR_SIZING=exact)


def league_shares(conn, agent):
    return {p["ticker"]: p["shares"] for p in ledger.positions(conn, agent)}


def test_sizing_setting(monkeypatch):
    monkeypatch.delenv("MIRROR_SIZING", raising=False)
    assert mirror.sizing() == "scaled"                                # default: unchanged behaviour
    monkeypatch.setenv("MIRROR_SIZING", " Exact ")
    assert mirror.sizing() == "exact"
    monkeypatch.setenv("MIRROR_SIZING", "huge")
    assert mirror.sizing() == "scaled"


def test_exact_combined_copies_the_league_share_counts(conn, monkeypatch):
    agents_with(conn, {"AAPL": 0.25}, {"XOM": 0.2})
    monkeypatch.setenv("MIRROR_MODE", "combined")
    monkeypatch.setenv("MIRROR_SIZING", "exact")
    b = FakeBroker(100_000)                                           # account size no longer matters
    mirror.sync_all(conn, DAYS[2], brokers={"combined": b})
    orders = dict(b.orders)
    assert orders["AAPL"] == pytest.approx(league_shares(conn, "claude")["AAPL"], rel=1e-6)   # ~2.5 sh, ~$250
    assert orders["XOM"] == pytest.approx(league_shares(conn, "gpt")["XOM"], rel=1e-6)        # ~4 sh, ~$200
    held = {(r["agent_id"], r["ticker"]): r["qty"] for r in conn.execute("SELECT * FROM mirror_holdings")}
    assert held[("claude", "AAPL")] == pytest.approx(league_shares(conn, "claude")["AAPL"], rel=1e-6)


def test_switching_to_exact_shrinks_scaled_positions(conn, monkeypatch):
    agents_with(conn, {"AAPL": 0.25}, {"XOM": 0.2})
    monkeypatch.setenv("MIRROR_MODE", "combined")
    b = FakeBroker(100_000)
    mirror.sync_all(conn, DAYS[2], brokers={"combined": b})          # scaled first: ~125 AAPL
    monkeypatch.setenv("MIRROR_SIZING", "exact")
    b2 = FakeBroker(100_000, positions=b.positions())
    mirror.sync_all(conn, DAYS[2], brokers={"combined": b2})
    assert b2.positions()["AAPL"] == pytest.approx(league_shares(conn, "claude")["AAPL"], rel=1e-6)
    assert dict(b2.orders)["AAPL"] < 0                                 # sold down to the league's size


def test_exact_each_mode_and_dashboard(conn, monkeypatch):
    from arena.api import queries
    two_agents_trading(conn)
    monkeypatch.setenv("MIRROR_MODE", "each")
    monkeypatch.setenv("MIRROR_SIZING", "exact")
    brokers = {"claude": FakeBroker(100_000), "gpt": FakeBroker(50_000)}
    mirror.sync_all(conn, DAYS[2], brokers=brokers)
    assert dict(brokers["gpt"].orders)["XOM"] == pytest.approx(league_shares(conn, "gpt")["XOM"], rel=1e-6)
    assert queries.mirror_overview(conn)["sizing"] == "exact"


@pytest.mark.parametrize("mode", ["combined", "leader", "each"])
def test_halt_blocks_every_alpaca_order_until_resume(conn, monkeypatch, mode):
    from arena import league
    agents_with(conn, {"AAPL": 0.25}, {"XOM": 0.2})
    conn.execute("INSERT INTO scores VALUES (1, 'gpt', 0.02, 1, 1000, 1020)")    # gpt leads
    monkeypatch.setenv("MIRROR_MODE", mode)
    brokers = {"combined": FakeBroker(100_000), "claude": FakeBroker(100_000), "gpt": FakeBroker(100_000)}
    league.set_halted(conn)
    assert league.is_halted(conn) and league.is_paused(conn)
    assert mirror.sync_all(conn, DAYS[2], brokers=brokers) == {}
    assert mirror.sync(conn, DAYS[2], brokers["gpt"], agent="gpt") == []
    assert mirror.sync_combined(conn, DAYS[2], brokers["combined"]) == {}
    assert not any(b.orders for b in brokers.values())
    assert conn.execute("SELECT COUNT(*) FROM events_log WHERE message LIKE '%HALTED%'").fetchone()[0] >= 1

    league.set_paused(conn, False)                    # global resume lifts the halt too
    assert not league.is_halted(conn) and not league.is_paused(conn)
    mirror.sync_all(conn, DAYS[2], brokers=brokers)
    assert any(b.orders for b in brokers.values())


def test_cancel_all_open_orders_reports_each_account_and_never_raises(monkeypatch):
    class Fake:
        def __init__(self, key=None, secret=None):
            if key == "bad":
                raise RuntimeError("unauthorized")
            self.key = key or "main"

        def account_id(self):
            return self.key

        def cancel_open_orders(self):
            return 3

    monkeypatch.setattr(mirror, "AlpacaPaperBroker", Fake)
    monkeypatch.setenv("MIRROR_MODE", "each")
    monkeypatch.setenv("ALPACA_MIRROR_GPT_KEY", "bad")
    monkeypatch.setenv("ALPACA_MIRROR_GPT_SECRET", "x")
    monkeypatch.setenv("ALPACA_MIRROR_CLAUDE_KEY", "main")      # same account as main: counted once
    monkeypatch.setenv("ALPACA_MIRROR_CLAUDE_SECRET", "x")
    out = mirror.cancel_all_open_orders(["claude", "gpt"])
    assert out["main"] == "3 open order(s) cancelled"
    assert "claude" not in out
    assert out["gpt"].startswith("FAILED") and "unauthorized" in out["gpt"]
