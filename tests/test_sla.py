from datetime import datetime, timedelta

import pytest

from sla import (
    SLA_HOURS, compute_due_at, format_ist, is_breached, ist_day_start_utc, ist_month_start_utc,
    sla_hours, time_left,
)

CREATED = datetime(2026, 1, 10, 6, 0, 0)  # naive UTC


@pytest.mark.parametrize(
    "category, hours",
    [("SECURITY", 4), ("TRANSPORT", 24), ("HOUSEKEEPING", 24), ("MAINTENANCE", 48),
     ("MEDICAL", 2), ("OTHERS", 72)],
)
def test_category_hours(category, hours):
    assert SLA_HOURS[category] == hours
    assert sla_hours(category, "NORMAL") == hours
    assert compute_due_at(CREATED, category, "NORMAL") == CREATED + timedelta(hours=hours)


@pytest.mark.parametrize("category", list(SLA_HOURS))
def test_urgent_halves_hours(category):
    assert sla_hours(category, "URGENT") == SLA_HOURS[category] / 2
    assert compute_due_at(CREATED, category, "URGENT") == CREATED + timedelta(hours=SLA_HOURS[category] / 2)


@pytest.mark.parametrize("priority", ["LOW", "NORMAL", "HIGH"])
def test_other_priorities_use_category_hours(priority):
    assert sla_hours("SECURITY", priority) == 4


def test_unknown_category_rejected():
    with pytest.raises(ValueError):
        sla_hours("PARKING", "NORMAL")


def test_is_breached():
    due = CREATED + timedelta(hours=4)
    assert not is_breached(due, now=due - timedelta(minutes=1))
    assert is_breached(due, now=due + timedelta(minutes=1))
    # Once resolved, only the resolution time matters.
    assert not is_breached(due, now=due + timedelta(days=5), resolved_at=due - timedelta(minutes=5))
    assert is_breached(due, resolved_at=due + timedelta(minutes=5))


def test_time_left():
    due = CREATED + timedelta(hours=4)
    assert time_left(due, now=CREATED) == ("4h 0m", False)
    assert time_left(due, now=due - timedelta(minutes=30)) == ("30m", False)
    assert time_left(due, now=due + timedelta(days=1, hours=2)) == ("Overdue by 1d 2h", True)


def test_ist_display():
    # 06:00 UTC is 11:30 in Asia/Kolkata.
    assert format_ist(CREATED) == "10 Jan 2026 11:30 IST"
    assert format_ist(None) == ""


def test_ist_period_starts():
    # 20:00 UTC on 31 Jan is already 1 Feb 01:30 IST.
    now = datetime(2026, 1, 31, 20, 0, 0)
    assert ist_day_start_utc(now) == datetime(2026, 1, 31, 18, 30)
    assert ist_month_start_utc(now) == datetime(2026, 1, 31, 18, 30)
