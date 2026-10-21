from datetime import timedelta

from conftest import PASSWORD, login, make_ticket, user_id
from models import EmailOutbox, Ticket, TicketEvent, User, db
from sla import utcnow

NEW_TICKET = {
    "title": "Broken chair",
    "category": "MAINTENANCE",
    "priority": "HIGH",
    "department": "SSG",
    "location": "Block B, 3F, desk 12",
    "description": "Private: the chair wobbles badly",
}


def test_login_required_redirects(client):
    resp = client.get("/")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    assert client.get("/api/tickets").status_code == 401


def test_login_and_logout(client):
    assert client.post("/login", data={"username": "emp1", "password": "wrong"}).status_code == 401
    resp = login(client, "emp1")
    assert resp.status_code == 302
    cookie = resp.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert client.get("/").status_code == 200
    client.get("/logout")
    assert client.get("/").status_code == 302


def test_login_rejects_external_next(client):
    resp = client.post("/login", data={"username": "emp1", "password": PASSWORD, "next": "//evil.com"})
    assert resp.headers["Location"] == "/"


def test_employee_creates_ticket(app, client):
    login(client, "emp1")
    resp = client.post("/tickets/new", data=NEW_TICKET)
    assert resp.status_code == 302
    with app.app_context():
        ticket = Ticket.query.one()
        assert ticket.ticket_no == f"FT-{ticket.id:06d}"
        assert ticket.status == "NEW"
        assert ticket.due_at - ticket.created_at == timedelta(hours=48)
        assert ticket.requester_id == user_id("emp1")
        assert [e.event_type for e in ticket.events] == ["CREATED"]


def test_ticket_validation(app, client):
    login(client, "emp1")
    data = dict(NEW_TICKET, title="", category="PARKING")
    resp = client.post("/tickets/new", data=data)
    assert resp.status_code == 400
    assert b"This field is required." in resp.data and b"Choose a valid category." in resp.data
    resp = client.post("/tickets/new", data=dict(NEW_TICKET, title="x" * 201))
    assert resp.status_code == 400
    with app.app_context():
        assert Ticket.query.count() == 0


def test_employee_sees_only_own_tickets(app, client):
    with app.app_context():
        own = make_ticket(requester="emp1")
        other = make_ticket(requester="emp2")
        other_no = db.session.get(Ticket, other).ticket_no
    login(client, "emp1")
    assert client.get(f"/tickets/{own}").status_code == 200
    assert client.get(f"/tickets/{other}").status_code == 404
    assert client.get("/tickets/9999").status_code == 404
    assert other_no.encode() not in client.get("/").data


def test_employee_cannot_use_agent_routes(app, client):
    with app.app_context():
        tid = make_ticket(requester="emp1")
    login(client, "emp1")
    assert client.post(f"/tickets/{tid}/status", data={"status": "CLOSED"}).status_code == 403
    assert client.post(f"/tickets/{tid}/assign", data={"team": "HOUSEKEEPING"}).status_code == 403
    assert client.get("/api/tickets").status_code == 403
    with app.app_context():
        assert db.session.get(Ticket, tid).status == "NEW"


def test_staff_cannot_raise_ticket(client):
    login(client, "admin")
    assert client.get("/tickets/new").status_code == 403
    client.get("/logout")
    login(client, "agent")
    assert client.get("/tickets/new").status_code == 403


def test_assign_requires_valid_team(app, client):
    with app.app_context():
        tid = make_ticket()
    login(client, "admin")
    assert client.post(f"/tickets/{tid}/assign", data={"team": "PARKING"}).status_code == 400
    assert client.post(f"/tickets/{tid}/assign", data={}).status_code == 400


def test_agent_sees_all_and_filters(app, client):
    with app.app_context():
        make_ticket(requester="emp1", category="SECURITY")
        make_ticket(requester="emp2", category="TRANSPORT")
        make_ticket(requester="emp2", category="OTHERS", created_at=utcnow() - timedelta(days=4))
    login(client, "admin")
    assert client.get("/").status_code == 200

    data = client.get("/api/tickets").get_json()
    assert len(data["tickets"]) == 3
    assert data["counts"]["open"] == 3
    assert data["counts"]["overdue"] == 1

    assert [t["category"] for t in client.get("/api/tickets?category=SECURITY").get_json()["tickets"]] == ["SECURITY"]
    breached = client.get("/api/tickets?breached=1").get_json()["tickets"]
    assert [t["category"] for t in breached] == ["OTHERS"]
    assert breached[0]["overdue"] is True
    assert client.get("/api/tickets?status=CLOSED").get_json()["tickets"] == []


def test_csrf_enforced(app):
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    resp = client.post("/login", data={"username": "emp1", "password": PASSWORD})
    assert resp.status_code == 400


def test_output_is_escaped(app, client):
    login(client, "emp1")
    client.post("/tickets/new", data=dict(NEW_TICKET, title="<script>alert(1)</script>"))
    with app.app_context():
        tid = Ticket.query.one().id
    body = client.get(f"/tickets/{tid}").data
    assert b"<script>alert(1)</script>" not in body
    assert b"&lt;script&gt;" in body


def test_email_failure_does_not_block_and_can_be_resent(app, client, monkeypatch):
    login(client, "emp1")
    assert client.post("/tickets/new", data=NEW_TICKET).status_code == 302
    with app.app_context():
        entries = EmailOutbox.query.all()
        assert {e.recipients for e in entries} == {"emp1@example.com", "maintenance@example.com"}
        assert all(e.status == "PENDING" and e.attempts == 1 and e.last_error for e in entries)
        # The description must never be emailed.
        assert all("wobbles" not in e.body for e in entries)
        assert all("http://tracker.test/tickets/" in e.body for e in entries)

    sent_messages = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: sent_messages.append(to))
    result = app.test_cli_runner().invoke(args=["resend-failed"])
    assert "Resent 2 email(s); 0 still pending." in result.output
    assert len(sent_messages) == 2
    with app.app_context():
        assert EmailOutbox.query.filter_by(status="SENT").count() == 2


def test_status_change_and_assign_send_emails(app, client, monkeypatch):
    sent = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: sent.append((to, subject)))
    with app.app_context():
        tid = make_ticket(requester="emp1")
    login(client, "admin")
    client.post(f"/tickets/{tid}/assign", data={"team": "TRANSPORT"})
    client.post(f"/tickets/{tid}/status", data={"status": "ON_HOLD"})
    recipients = [to for to, _ in sent]
    assert recipients == [
        ["agent2@example.com", "transport@example.com"], ["emp1@example.com"], ["emp1@example.com"],
    ]
    assert "Transport team" in sent[0][1]
    assert "IN_PROGRESS" in sent[1][1] and "ON_HOLD" in sent[-1][1]
    with app.app_context():
        assert TicketEvent.query.filter_by(ticket_id=tid).count() == 4


def test_branding_in_header(client):
    page = client.get("/login").data
    assert b"FAXORA" in page and b"Facility Tracker" in page


def test_test_email_command(app, monkeypatch):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["test-email", "someone@example.com"])
    assert result.exit_code != 0 and "SMTP_HOST is not configured" in result.output
    sent = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: sent.append(to))
    result = runner.invoke(args=["test-email", "someone@example.com"])
    assert result.exit_code == 0 and sent == [["someone@example.com"]]
    with app.app_context():
        assert EmailOutbox.query.count() == 0


def test_set_email_command(app):
    runner = app.test_cli_runner()
    assert runner.invoke(args=["set-email", "emp1", "real@example.org"]).exit_code == 0
    assert runner.invoke(args=["set-email", "nobody", "x@example.org"]).exit_code != 0
    with app.app_context():
        assert db.session.get(User, user_id("emp1")).email == "real@example.org"
