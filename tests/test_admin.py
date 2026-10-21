from conftest import login, make_ticket, user_id
from models import EmailOutbox, Ticket, TicketEvent, User, db

TICKET = {
    "title": "Desk lamp broken", "category": "MAINTENANCE", "priority": "NORMAL",
    "department": "CYBER", "location": "Block D", "description": "Lamp flickers",
}


def test_department_must_be_from_list(app, client):
    login(client, "emp1")
    page = client.get("/tickets/new").data
    for dept in (b"DPS", b"SSG", b"SSI", b"FSI", b"CYBER"):
        assert b'<option value="' + dept + b'"' in page
    resp = client.post("/tickets/new", data=dict(TICKET, department="Finance"))
    assert resp.status_code == 400 and b"Choose a valid department." in resp.data
    assert client.post("/tickets/new", data=TICKET).status_code == 302


def test_new_ticket_status_form_does_not_preselect(app, client):
    with app.app_context():
        tid = make_ticket()
    login(client, "admin")
    page = client.get(f"/tickets/{tid}").data
    assert b"current: NEW" in page
    assert b'<option value="" disabled selected>Choose new status' in page
    assert b"ASSIGNED</option>" not in page


def test_assign_lists_all_teams(app, client):
    with app.app_context():
        tid = make_ticket(category="TRANSPORT")
    login(client, "admin")
    page = client.get(f"/tickets/{tid}").data.decode()
    for team in ("TRANSPORT", "SECURITY", "HOUSEKEEPING", "MAINTENANCE", "MEDICAL", "OTHERS"):
        assert f'<option value="{team}"' in page
    assert "TRANSPORT (1 agent)" in page and "SECURITY (0 agents)" in page


def test_assigned_team_shown(app, client):
    with app.app_context():
        tid = make_ticket()
    login(client, "admin")
    client.post(f"/tickets/{tid}/assign", data={"team": "TRANSPORT"})
    rows = client.get("/api/tickets").get_json()["tickets"]
    assert rows[0]["team"] == "Transport" and rows[0]["status"] == "IN_PROGRESS"


def test_admin_deletes_ticket_in_console(app, client):
    login(client, "emp1")
    client.post("/tickets/new", data=TICKET)  # leaves two PENDING emails (no SMTP in tests)
    client.get("/logout")
    with app.app_context():
        tid = Ticket.query.one().id
    login(client, "admin")
    resp = client.post(f"/admin/tickets/{tid}/delete", data={"reason": ""}, follow_redirects=True)
    assert b"reason for deleting" in resp.data
    with app.app_context():
        assert db.session.get(Ticket, tid).deleted_at is None
    resp = client.post(f"/admin/tickets/{tid}/delete", data={"reason": "Test ticket"})
    assert resp.status_code == 302
    assert client.post(f"/admin/tickets/{tid}/delete", data={"reason": "again"}).status_code == 400
    page = client.get("/admin/tickets?show=deleted").data
    assert TICKET["title"].encode() in page and b"by Admin" in page
    assert TICKET["title"].encode() not in client.get("/admin/tickets").data

    assert client.get(f"/tickets/{tid}").status_code == 404
    data = client.get("/api/tickets").get_json()
    assert data["tickets"] == [] and data["counts"]["open"] == 0 and data["counts"]["total_month"] == 0
    assert client.post(f"/tickets/{tid}/status", data={"status": "CLOSED"}).status_code == 404
    with app.app_context():
        ticket = db.session.get(Ticket, tid)
        assert ticket.deleted_at is not None and ticket.deleted_by_id == user_id("admin")
        event = TicketEvent.query.filter_by(ticket_id=tid, event_type="DELETED").one()
        assert event.comment == "Test ticket"
        assert {e.status for e in EmailOutbox.query} == {"CANCELLED"}
    client.get("/logout")
    login(client, "emp1")
    assert b"Desk lamp" not in client.get("/").data


def test_only_admin_console_can_delete(app, client):
    with app.app_context():
        tid = make_ticket(requester="emp1", team="HOUSEKEEPING")
    for username in ("emp1", "agent"):
        login(client, username)
        assert client.post(f"/admin/tickets/{tid}/delete", data={"reason": "x"}).status_code == 403
        client.get("/logout")
    login(client, "admin")
    # The old ticket-page delete route no longer exists.
    assert client.post(f"/tickets/{tid}/delete", data={"reason": "x"}).status_code in (404, 405)
    page = client.get(f"/tickets/{tid}").data
    assert b"Delete ticket" not in page and b"Cancel ticket" in page
    with app.app_context():
        assert db.session.get(Ticket, tid).deleted_at is None


def test_agent_cancels_own_team_ticket(app, client):
    with app.app_context():
        tid = make_ticket(requester="emp1", team="HOUSEKEEPING", status="IN_PROGRESS")
    login(client, "agent")
    assert b"Cancel ticket" in client.get(f"/tickets/{tid}").data
    resp = client.post(f"/tickets/{tid}/status", data={"status": "CANCELLED", "comment": "Raised twice"})
    assert resp.status_code == 302
    with app.app_context():
        assert db.session.get(Ticket, tid).status == "CANCELLED"
    client.get("/logout")
    login(client, "emp1")
    assert b"CANCELLED" in client.get(f"/tickets/{tid}").data


def test_create_user_and_set_team_commands(app):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=["create-user", "hk1", "--name", "Hk One", "--email", "hk1@example.com",
              "--role", "agent", "--team", "housekeeping"],
        input="long-password\nlong-password\n",
    )
    assert result.exit_code == 0, result.output
    assert runner.invoke(args=["set-team", "hk1", "security"]).exit_code == 0
    assert runner.invoke(args=["set-team", "emp1", "security"]).exit_code != 0
    assert runner.invoke(args=["set-role", "emp2", "admin"]).exit_code == 0
    bad = runner.invoke(
        args=["create-user", "e9", "--name", "E", "--email", "e@example.com", "--department", "HR"],
        input="long-password\nlong-password\n",
    )
    assert bad.exit_code != 0 and "Department must be one of" in bad.output
    with app.app_context():
        user = User.query.filter_by(username="hk1").one()
        assert user.team == "SECURITY" and user.check_password("long-password")
        assert User.query.filter_by(username="emp2").one().role == "admin"
