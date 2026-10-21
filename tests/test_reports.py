"""Monthly filter, CSV export, admin reports, and agents working by category."""
import csv
import io
from datetime import datetime

from conftest import login, make_ticket
from models import Ticket, db
from sla import ist_month_range_utc, parse_month, recent_months

# 05:00 UTC on 1 Sep is 10:30 IST on 1 Sep; 20:00 UTC on 31 Aug is 01:30 IST on 1 Sep.
SEP_EARLY = datetime(2026, 9, 1, 5, 0)
SEP_BOUNDARY = datetime(2026, 8, 31, 20, 0)
AUG = datetime(2026, 8, 15, 5, 0)


def read_csv(resp):
    text = resp.data.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


def test_month_helpers():
    assert parse_month("2026-09") == (2026, 9)
    assert parse_month("2026-13") is None and parse_month("x") is None and parse_month("") is None
    start, end = ist_month_range_utc(2026, 12)
    assert (start, end) == (datetime(2026, 11, 30, 18, 30), datetime(2026, 12, 31, 18, 30))
    months = recent_months(3, now=datetime(2026, 1, 10))
    assert months == [("2026-01", "Jan 2026"), ("2025-12", "Dec 2025"), ("2025-11", "Nov 2025")]


def test_dashboard_month_filter_uses_ist(app, client):
    with app.app_context():
        make_ticket(created_at=SEP_EARLY)
        make_ticket(created_at=SEP_BOUNDARY)
        make_ticket(created_at=AUG)
    login(client, "admin")
    sep = client.get("/api/tickets?month=2026-09").get_json()["tickets"]
    aug = client.get("/api/tickets?month=2026-08").get_json()["tickets"]
    assert len(sep) == 2 and len(aug) == 1
    assert len(client.get("/api/tickets?month=bad").get_json()["tickets"]) == 3
    assert b'<option value="' in client.get("/?month=2026-09").data


def test_export_csv_respects_filters_and_scope(app, client):
    with app.app_context():
        make_ticket(category="HOUSEKEEPING", created_at=SEP_EARLY)
        make_ticket(category="TRANSPORT", created_at=SEP_EARLY)
        make_ticket(category="HOUSEKEEPING", created_at=AUG)
    login(client, "admin")
    resp = client.get("/tickets/export.csv?month=2026-09")
    assert resp.mimetype == "text/csv"
    assert 'filename="faxora-tickets-2026-09.csv"' in resp.headers["Content-Disposition"]
    rows = read_csv(resp)
    assert rows[0][:3] == ["Ticket", "Created (IST)", "Category"] and len(rows) == 3
    client.get("/logout")
    login(client, "agent")  # Housekeeping
    rows = read_csv(client.get("/tickets/export.csv"))
    assert [r[2] for r in rows[1:]] == ["HOUSEKEEPING", "HOUSEKEEPING"]
    client.get("/logout")
    login(client, "emp1")
    assert client.get("/tickets/export.csv").status_code == 403


def test_export_blocks_formula_injection(app, client):
    with app.app_context():
        tid = make_ticket()
        db.session.get(Ticket, tid).title = "=HYPERLINK(\"http://evil\")"
        db.session.commit()
    login(client, "admin")
    rows = read_csv(client.get("/tickets/export.csv"))
    assert rows[1][10].startswith("'=")


def test_export_omits_description(app, client):
    with app.app_context():
        make_ticket()  # description: "Secret details that must not be emailed"
    login(client, "admin")
    assert b"Secret details" not in client.get("/tickets/export.csv").data


def test_admin_report_page_and_csv(app, client):
    with app.app_context():
        done = make_ticket(category="SECURITY", created_at=SEP_EARLY, team="SECURITY", status="RESOLVED")
        make_ticket(category="SECURITY", created_at=SEP_EARLY, status="CANCELLED")
        make_ticket(category="TRANSPORT", created_at=SEP_EARLY)
        make_ticket(category="TRANSPORT", created_at=AUG)
        db.session.get(Ticket, done).resolved_at = datetime(2026, 9, 1, 7, 0)  # 2 h after creation
        db.session.commit()
    login(client, "admin")
    page = client.get("/admin/reports?month=2026-09").data.decode()
    assert "Sep 2026" in page and "Tickets raised" in page
    rows = read_csv(client.get("/admin/reports/summary.csv?month=2026-09"))
    by_group = {r[0]: r[1:] for r in rows[1:]}
    assert by_group["All tickets"] == ["3", "1", "1", "1", "0", "100", "2.0"]
    assert by_group["Category: Security"][:4] == ["2", "0", "1", "1"]
    assert by_group["Team: Unassigned"][0] == "2"
    tickets = read_csv(client.get("/admin/reports/tickets.csv?month=2026-09"))
    assert len(tickets) == 4
    assert client.get("/admin/reports/other.csv").status_code == 404
    client.get("/logout")
    login(client, "agent")
    assert client.get("/admin/reports").status_code == 403


def test_agent_sees_unassigned_tickets_in_own_category(app, client):
    with app.app_context():
        mine = make_ticket(category="HOUSEKEEPING")                  # unassigned, my category
        moved = make_ticket(category="HOUSEKEEPING", team="TRANSPORT")  # reassigned away
        other = make_ticket(category="MAINTENANCE")                  # another category
    login(client, "agent")  # Housekeeping
    ids = {t["id"] for t in client.get("/api/tickets").get_json()["tickets"]}
    assert ids == {mine}
    assert client.get(f"/tickets/{moved}").status_code == 404
    assert client.get(f"/tickets/{other}").status_code == 404
    # Starting work assigns the ticket to the agent's team.
    assert client.post(f"/tickets/{mine}/status", data={"status": "IN_PROGRESS"}).status_code == 302
    with app.app_context():
        ticket = db.session.get(Ticket, mine)
        assert (ticket.status, ticket.assigned_team) == ("IN_PROGRESS", "HOUSEKEEPING")
    assert client.post(f"/tickets/{other}/status", data={"status": "IN_PROGRESS"}).status_code == 403
