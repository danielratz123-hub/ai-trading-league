import pytest

from arena import costs


def test_commission_minimum_per_share_and_cap():
    # 10 shares @ $100 = $1,000 value: per-share 0.035 < min 0.35 -> 0.35
    assert costs.commission(10, 100.0) == pytest.approx(0.35)
    # 1,000 shares @ $50: per-share 3.50 > min
    assert costs.commission(1000, 50.0) == pytest.approx(3.50)
    # $10 trade: min 0.35 would be 3.5%, cap at 1% = 0.10
    assert costs.commission(0.1, 100.0) == pytest.approx(0.10)
    # sign of shares doesn't matter
    assert costs.commission(-1000, 50.0) == costs.commission(1000, 50.0)


def test_slippage_always_against_you():
    assert costs.fill_price(100.0, +5) == pytest.approx(100.05)   # buy higher
    assert costs.fill_price(100.0, -5) == pytest.approx(99.95)    # sell / short lower


def test_minimum_trade_size():
    assert costs.is_too_small(0.009, 100.0)       # $0.90
    assert not costs.is_too_small(0.011, 100.0)   # $1.10


def test_borrow_fee():
    # $365 short for 1 day at 0.5%/yr = $0.005
    assert costs.borrow_fee(365.0, 1) == pytest.approx(0.005)
    assert costs.borrow_fee(-365.0, 3) == pytest.approx(0.015)
