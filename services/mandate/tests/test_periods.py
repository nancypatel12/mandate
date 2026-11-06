from datetime import UTC, datetime, timedelta, timezone

import pytest

from mandate.models import Period
from mandate.policy import period_start


def test_day_starts_at_utc_midnight():
    assert period_start(Period.DAY, datetime(2026, 11, 6, 23, 59, 59, tzinfo=UTC)) == datetime(2026, 11, 6, tzinfo=UTC)


def test_week_starts_monday():
    friday = datetime(2026, 11, 6, 15, tzinfo=UTC)
    assert period_start(Period.WEEK, friday) == datetime(2026, 11, 2, tzinfo=UTC)


def test_monday_is_its_own_week_start():
    monday = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    assert period_start(Period.WEEK, monday) == monday


def test_sunday_belongs_to_previous_monday():
    assert period_start(Period.WEEK, datetime(2026, 11, 8, 23, tzinfo=UTC)) == datetime(2026, 11, 2, tzinfo=UTC)


def test_month_starts_on_the_first():
    assert period_start(Period.MONTH, datetime(2026, 11, 30, 23, tzinfo=UTC)) == datetime(2026, 11, 1, tzinfo=UTC)


def test_windows_are_utc_regardless_of_input_zone():
    # 8pm on Nov 6 in New York is already Nov 7 in UTC.
    ny = timezone(timedelta(hours=-5))
    assert period_start(Period.DAY, datetime(2026, 11, 6, 20, tzinfo=ny)) == datetime(2026, 11, 7, tzinfo=UTC)


def test_next_day_is_a_new_window():
    a = period_start(Period.DAY, datetime(2026, 11, 6, 23, 59, tzinfo=UTC))
    b = period_start(Period.DAY, datetime(2026, 11, 7, 0, 0, tzinfo=UTC))
    assert b - a == timedelta(days=1)


def test_naive_datetime_is_refused():
    with pytest.raises(ValueError):
        period_start(Period.DAY, datetime(2026, 11, 6))
