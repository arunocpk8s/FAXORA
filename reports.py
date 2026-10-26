"""Ticket exports (CSV that opens in Excel) and monthly summary figures."""
import csv
import io

from config import CATEGORIES
from sla import format_ist

EXPORT_COLUMNS = [
    "Ticket", "Created (IST)", "Category", "Priority", "Status", "Team", "SOS", "Requester",
    "Department", "Location", "Seat", "Title", "Due (IST)", "Resolved (IST)", "Resolution hours", "SLA",
]
SUMMARY_COLUMNS = ["Group", "Created", "Open", "Resolved/closed", "Cancelled", "Breached", "SLA met %", "Avg resolution hours"]


def _cell(value):
    """Stop spreadsheet formula injection from user-entered text."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def resolution_hours(ticket):
    if not ticket.resolved_at:
        return None
    return round((ticket.resolved_at - ticket.created_at).total_seconds() / 3600, 1)


def sla_result(ticket):
    if ticket.status == "CANCELLED":
        return "Cancelled"
    if ticket.breached:
        return "Breached"
    return "Met" if ticket.status in ("RESOLVED", "CLOSED") else "Open"


def _csv(header, rows):
    buf = io.StringIO()
    buf.write("﻿")  # BOM so Excel reads UTF-8 correctly
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return buf.getvalue()


def tickets_csv(tickets):
    return _csv(EXPORT_COLUMNS, (
        [t.ticket_no, format_ist(t.created_at), t.category, t.priority, t.status,
         t.assigned_team or "", "Yes" if t.is_sos else "No", t.requester.full_name, t.department,
         t.location, t.seat_number or "", t.title, format_ist(t.due_at), format_ist(t.resolved_at),
         resolution_hours(t), sla_result(t)]
        for t in tickets
    ))


def _figures(tickets):
    resolved = [t for t in tickets if t.status in ("RESOLVED", "CLOSED")]
    met = [t for t in resolved if not t.breached]
    hours = [resolution_hours(t) for t in resolved if t.resolved_at]
    return {
        "created": len(tickets),
        "open": sum(1 for t in tickets if t.is_open),
        "resolved": len(resolved),
        "cancelled": sum(1 for t in tickets if t.status == "CANCELLED"),
        "breached": sum(1 for t in tickets if t.breached),
        "sla_met_pct": round(100 * len(met) / len(resolved)) if resolved else None,
        "avg_hours": round(sum(hours) / len(hours), 1) if hours else None,
    }


def summarize(tickets):
    """Totals plus figures per category and per assigned team."""
    return {
        "total": _figures(tickets),
        "by_category": [(c, _figures([t for t in tickets if t.category == c])) for c in CATEGORIES],
        "by_team": [(c, _figures([t for t in tickets if t.assigned_team == c])) for c in CATEGORIES]
                   + [("UNASSIGNED", _figures([t for t in tickets if t.assigned_team is None]))],
    }


def summary_csv(summary):
    def row(label, f):
        return [label, f["created"], f["open"], f["resolved"], f["cancelled"], f["breached"],
                "" if f["sla_met_pct"] is None else f["sla_met_pct"], "" if f["avg_hours"] is None else f["avg_hours"]]

    rows = [row("All tickets", summary["total"])]
    rows += [row(f"Category: {c.title()}", f) for c, f in summary["by_category"]]
    rows += [row(f"Team: {c.title()}", f) for c, f in summary["by_team"]]
    return _csv(SUMMARY_COLUMNS, rows)
