"""SLA rules: resolve-by hours per category, due-date calculation and time helpers.

All datetimes are stored as naive UTC. They are shown to users in Asia/Kolkata (UTC+05:30).
"""
from datetime import datetime, timedelta, timezone

# Resolve-by hours per category. Change SLAs here.
SLA_HOURS = {
    "SECURITY": 4,
    "TRANSPORT": 24,
    "HOUSEKEEPING": 24,
    "MAINTENANCE": 48,
    "MEDICAL": 2,
    "OTHERS": 72,
}

# SOS (urgent help) tickets must be handled within this many hours.
SOS_SLA_HOURS = 0.25

# Multiplier applied to the category hours for a priority (missing = 1).
PRIORITY_FACTOR = {
    "URGENT": 0.5,
}

# India has no daylight saving, so a fixed offset is exact for Asia/Kolkata.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def utcnow():
    """Current UTC time as a naive datetime (the form stored in the database)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sla_hours(category, priority):
    if category not in SLA_HOURS:
        raise ValueError(f"Unknown category: {category}")
    return SLA_HOURS[category] * PRIORITY_FACTOR.get(priority, 1)


def compute_due_at(created_at, category, priority):
    return created_at + timedelta(hours=sla_hours(category, priority))


def compute_sos_due_at(created_at):
    return created_at + timedelta(hours=SOS_SLA_HOURS)


def is_breached(due_at, now=None, resolved_at=None):
    """True if resolved after due_at, or (when unresolved) still open past due_at."""
    if due_at is None:
        return False
    reference = resolved_at or now or utcnow()
    return reference > due_at


def time_left(due_at, now=None):
    """Return (text, overdue) describing the time remaining until due_at."""
    now = now or utcnow()
    seconds = int((due_at - now).total_seconds())
    overdue = seconds < 0
    days, rem = divmod(abs(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        text = f"{days}d {hours}h"
    elif hours:
        text = f"{hours}h {minutes}m"
    else:
        text = f"{minutes}m"
    return (f"Overdue by {text}" if overdue else text), overdue


def to_ist(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)


def format_ist(dt):
    return to_ist(dt).strftime("%d %b %Y %H:%M IST") if dt else ""


def ist_day_start_utc(now=None):
    """Start of the current IST day, as naive UTC."""
    local = to_ist(now or utcnow())
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def parse_month(value):
    """'2026-10' -> (2026, 10), or None if not a valid month."""
    try:
        year, month = (int(part) for part in value.split("-"))
    except (AttributeError, ValueError):
        return None
    return (year, month) if 2000 <= year <= 2100 and 1 <= month <= 12 else None


def ist_month_range_utc(year, month):
    """[start, end) of an IST calendar month, as naive UTC."""
    start = datetime(year, month, 1, tzinfo=IST)
    end = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=IST)
    return (start.astimezone(timezone.utc).replace(tzinfo=None),
            end.astimezone(timezone.utc).replace(tzinfo=None))


def recent_months(count=12, now=None):
    """[('2026-10', 'Oct 2026'), ...] newest first, in IST."""
    local = to_ist(now or utcnow())
    year, month = local.year, local.month
    months = []
    for _ in range(count):
        months.append((f"{year:04d}-{month:02d}", datetime(year, month, 1).strftime("%b %Y")))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return months


def ist_month_start_utc(now=None):
    """Start of the current IST month, as naive UTC."""
    local = to_ist(now or utcnow())
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).replace(tzinfo=None)
