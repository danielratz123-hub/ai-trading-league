"""Trading costs. Pure functions, identical for every agent."""
from __future__ import annotations

from .config import SETTINGS, Costs


def fill_price(ref_price: float, shares: float, costs: Costs = SETTINGS.costs) -> float:
    """Slippage always works against you: buys fill higher, sells lower."""
    slip = costs.slippage_bps / 10_000
    return ref_price * (1 + slip) if shares > 0 else ref_price * (1 - slip)


def commission(shares: float, fill: float, costs: Costs = SETTINGS.costs) -> float:
    value = abs(shares) * fill
    fee = max(costs.commission_min, costs.commission_per_share * abs(shares))
    # The 1% cap matters for small fractional trades: a $10 trade would
    # otherwise pay 3.5% just in minimum commission.
    return min(fee, costs.commission_max_pct * value)


def is_too_small(shares: float, ref_price: float, costs: Costs = SETTINGS.costs) -> bool:
    return abs(shares) * ref_price < costs.min_trade_value


def borrow_fee(short_value: float, days: int, costs: Costs = SETTINGS.costs) -> float:
    """Fee for holding a short of `short_value` dollars for `days` calendar
    days. Calendar days (not trading days) so weekends are charged too, as
    a real broker would."""
    return abs(short_value) * costs.borrow_fee_annual * days / 365
