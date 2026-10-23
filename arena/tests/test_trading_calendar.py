"""Calendar helpers use stored sessions only; no Alpaca calls or credentials."""
import pytest

from arena import trading_calendar as calendar


@pytest.fixture
def calendar_conn(conn):
    # July 3 is a half day, July 4 a holiday, and July 6-7 a weekend.
    conn.execute("DELETE FROM calendar")
    calendar.store_days(conn, [
        ("2024-07-08", "09:30", "16:00"),
        ("2024-07-05", "09:30", "16:00"),
        ("2024-07-03", "09:30", "13:00"),
        ("2024-07-02", "09:30", "16:00"),
    ])
    return conn


@pytest.mark.parametrize("day, expected", [
    ("2024-07-02", True),
    ("2024-07-03", True),
    ("2024-07-04", False),
    ("2024-07-06", False),
    ("2024-07-07", False),
    ("2024-07-08", True),
    ("2024-07-01", False),
    ("2024-07-09", False),
])
def test_is_trading_day(calendar_conn, day, expected):
    assert calendar.is_trading_day(calendar_conn, day) is expected


@pytest.mark.parametrize("day, expected", [
    ("2024-07-01", "2024-07-02"),
    ("2024-07-02", "2024-07-03"),
    ("2024-07-03", "2024-07-05"),
    ("2024-07-04", "2024-07-05"),
    ("2024-07-05", "2024-07-08"),
    ("2024-07-06", "2024-07-08"),
    ("2024-07-07", "2024-07-08"),
])
def test_next_trading_day_is_strictly_after(calendar_conn, day, expected):
    assert calendar.next_trading_day(calendar_conn, day) == expected


@pytest.mark.parametrize("day, expected", [
    ("2024-07-03", "2024-07-02"),
    ("2024-07-04", "2024-07-03"),
    ("2024-07-05", "2024-07-03"),
    ("2024-07-06", "2024-07-05"),
    ("2024-07-07", "2024-07-05"),
    ("2024-07-08", "2024-07-05"),
    ("2024-07-09", "2024-07-08"),
])
def test_previous_trading_day_is_strictly_before(calendar_conn, day, expected):
    assert calendar.previous_trading_day(calendar_conn, day) == expected


@pytest.mark.parametrize("day, expected", [
    ("2024-07-02", "2024-07-02"),
    ("2024-07-03", "2024-07-03"),
    ("2024-07-04", "2024-07-03"),
    ("2024-07-06", "2024-07-05"),
    ("2024-07-07", "2024-07-05"),
    ("2024-07-08", "2024-07-08"),
    ("2024-07-09", "2024-07-08"),
])
def test_last_trading_day_on_or_before_is_inclusive(calendar_conn, day, expected):
    assert calendar.last_trading_day_on_or_before(calendar_conn, day) == expected


@pytest.mark.parametrize("helper, day, message", [
    (calendar.next_trading_day, "2024-07-08", "No trading day after 2024-07-08"),
    (calendar.next_trading_day, "2024-07-09", "No trading day after 2024-07-09"),
    (calendar.previous_trading_day, "2024-07-02", "No trading day before 2024-07-02"),
    (calendar.previous_trading_day, "2024-07-01", "No trading day before 2024-07-01"),
    (calendar.last_trading_day_on_or_before, "2024-07-01", "No trading day on or before 2024-07-01"),
])
def test_navigation_raises_when_no_matching_session(calendar_conn, helper, day, message):
    with pytest.raises(RuntimeError, match=message):
        helper(calendar_conn, day)


@pytest.mark.parametrize("start, end, expected", [
    ("2024-07-02", "2024-07-08", ["2024-07-02", "2024-07-03", "2024-07-05", "2024-07-08"]),
    ("2024-07-01", "2024-07-09", ["2024-07-02", "2024-07-03", "2024-07-05", "2024-07-08"]),
    ("2024-07-03", "2024-07-03", ["2024-07-03"]),
    ("2024-07-04", "2024-07-07", ["2024-07-05"]),
    ("2024-07-04", "2024-07-04", []),
    ("2024-07-06", "2024-07-07", []),
    ("2024-07-09", "2024-07-10", []),
    ("2024-07-08", "2024-07-02", []),
])
def test_trading_days_are_sorted_and_include_both_endpoints(calendar_conn, start, end, expected):
    assert calendar.trading_days(calendar_conn, start, end) == expected


@pytest.mark.parametrize("start, end, expected", [
    ("2024-07-03", "2024-07-03", 0),
    ("2024-07-03", "2024-07-08", 5),  # Counts holidays and weekends too.
    ("2024-07-08", "2024-07-03", -5),
    ("2024-02-28", "2024-03-01", 2),  # Leap day.
    ("2023-12-31", "2024-01-01", 1),
])
def test_calendar_days_between(start, end, expected):
    assert calendar.calendar_days_between(start, end) == expected


def test_require_calendar_accepts_populated_table(calendar_conn):
    assert calendar._require_calendar(calendar_conn) is None


@pytest.mark.parametrize("helper, args", [
    (calendar._require_calendar, ()),
    (calendar.is_trading_day, ("2024-07-03",)),
    (calendar.next_trading_day, ("2024-07-03",)),
    (calendar.previous_trading_day, ("2024-07-03",)),
    (calendar.last_trading_day_on_or_before, ("2024-07-03",)),
    (calendar.trading_days, ("2024-07-02", "2024-07-08")),
])
def test_empty_calendar_raises_with_update_instructions(conn, helper, args):
    conn.execute("DELETE FROM calendar")
    with pytest.raises(RuntimeError, match="Trading calendar is empty.*update-calendar"):
        helper(conn, *args)
