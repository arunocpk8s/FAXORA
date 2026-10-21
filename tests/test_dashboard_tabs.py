"""Open / Resolved-closed / Cancelled / All tabs on the staff dashboard."""
from datetime import datetime

from conftest import login, make_ticket


def make_mix(app):
    with app.app_context():
        make_ticket(category="HOUSEKEEPING", status="NEW")
        make_ticket(category="HOUSEKEEPING", status="IN_PROGRESS", team="HOUSEKEEPING")
        make_ticket(category="HOUSEKEEPING", status="RESOLVED", team="HOUSEKEEPING")
        make_ticket(category="HOUSEKEEPING", status="CANCELLED", team="HOUSEKEEPING")
        make_ticket(category="TRANSPORT", status="CANCELLED", team="TRANSPORT")


def statuses(client, query=""):
    return sorted(t["status"] for t in client.get(f"/api/tickets{query}").get_json()["tickets"])


def test_tabs_filter_by_status_group(app, client):
    make_mix(app)
    login(client, "admin")
    assert statuses(client, "?view=open") == ["IN_PROGRESS", "NEW"]
    assert statuses(client, "?view=done") == ["RESOLVED"]
    assert statuses(client, "?view=cancelled") == ["CANCELLED", "CANCELLED"]
    assert len(statuses(client, "?view=all")) == 5
    assert len(statuses(client, "?view=bogus")) == 5


def test_agent_cancelled_tab_shows_only_own_team(app, client):
    make_mix(app)
    login(client, "agent")  # Housekeeping
    assert statuses(client, "?view=cancelled") == ["CANCELLED"]
    page = client.get("/?view=cancelled").data.decode()
    assert 'class="active" aria-current="page">Cancelled <span class="tab-count">1</span>' in page
    assert 'Open <span class="tab-count">2</span>' in page


def test_tabs_keep_other_filters_and_export(app, client):
    with app.app_context():
        make_ticket(status="CANCELLED", created_at=datetime(2026, 9, 2, 5, 0))
        make_ticket(status="CANCELLED", created_at=datetime(2026, 8, 2, 5, 0))
    login(client, "admin")
    page = client.get("/?view=cancelled&month=2026-09").data.decode()
    assert 'Cancelled <span class="tab-count">1</span>' in page
    assert "view=open" in page and "month=2026-09" in page
    rows = client.get("/tickets/export.csv?view=cancelled&month=2026-09").data.decode("utf-8-sig").strip().splitlines()
    assert len(rows) == 2


def test_employee_dashboard_has_no_tabs(client):
    login(client, "emp1")
    assert b"tab-count" not in client.get("/").data
