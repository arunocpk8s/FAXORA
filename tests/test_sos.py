import sqlite3
from datetime import timedelta

from app import create_app
from conftest import login, make_ticket
from models import EmailOutbox, Ticket, db

SOS = {"location": "Block A, 4F, near lift", "phone": "98765 43210", "note": "Colleague fainted"}


def test_sos_creates_urgent_medical_ticket(app, client):
    login(client, "emp1")
    resp = client.post("/sos", data=SOS)
    assert resp.status_code == 302
    with app.app_context():
        ticket = Ticket.query.one()
        assert ticket.is_sos and ticket.category == "MEDICAL" and ticket.priority == "URGENT"
        assert ticket.assigned_team == "MEDICAL"
        assert ticket.status == "NEW"
        assert ticket.due_at - ticket.created_at == timedelta(minutes=15)
        assert ticket.location == SOS["location"]
        assert "98765 43210" in ticket.description and "fainted" in ticket.description
        assert ticket.events[0].comment == "SOS raised"


def test_sos_alerts_responders_without_health_details(app, client):
    login(client, "emp1")
    client.post("/sos", data=SOS)
    with app.app_context():
        alert, confirmation = EmailOutbox.query.order_by(EmailOutbox.id).all()
        assert set(alert.recipients.split(",")) == {
            "security@example.com", "medical@example.com", "agent@example.com", "agent2@example.com",
            "admin@example.com",
        }
        assert alert.subject.startswith("SOS [FT-")
        assert SOS["location"] in alert.body and SOS["phone"] in alert.body
        assert "fainted" not in alert.body
        assert confirmation.recipients == "emp1@example.com"
        assert "112" in confirmation.body


def test_sos_requires_location(app, client):
    login(client, "emp1")
    resp = client.post("/sos", data={"location": "  ", "note": "help"})
    assert resp.status_code == 400
    with app.app_context():
        assert Ticket.query.count() == 0


def test_repeat_sos_reuses_active_one(app, client):
    login(client, "emp1")
    client.post("/sos", data=SOS)
    resp = client.post("/sos", data=SOS)
    assert resp.status_code == 302
    with app.app_context():
        assert Ticket.query.count() == 1


def test_agent_can_raise_sos(app, client):
    login(client, "agent")
    assert client.get("/sos").status_code == 200
    assert client.post("/sos", data=SOS).status_code == 302


def test_sos_requires_login(client):
    assert client.get("/sos").status_code == 302


def test_agent_dashboard_shows_sos_first(app, client):
    with app.app_context():
        make_ticket(requester="emp2")
    login(client, "emp1")
    client.post("/sos", data=SOS)
    client.get("/logout")
    with app.app_context():
        make_ticket(requester="emp2")  # newer, but not SOS
    login(client, "admin")
    data = client.get("/api/tickets").get_json()
    assert data["counts"]["sos"] == 1
    assert data["tickets"][0]["is_sos"] is True
    page = client.get("/").data
    assert b"1 active SOS" in page and SOS["location"].encode() in page


def test_employee_cannot_see_others_sos(app, client):
    login(client, "emp1")
    client.post("/sos", data=SOS)
    client.get("/logout")
    with app.app_context():
        tid = Ticket.query.one().id
    login(client, "emp2")
    assert client.get(f"/tickets/{tid}").status_code == 404


def test_existing_database_is_upgraded(tmp_path):
    """A database from the first release gets the new columns, and ASSIGNED becomes IN_PROGRESS."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE,"
            " full_name VARCHAR(120) NOT NULL, email VARCHAR(200) NOT NULL, department VARCHAR(120) NOT NULL,"
            " role VARCHAR(20) NOT NULL, password_hash VARCHAR(255) NOT NULL, active BOOLEAN NOT NULL)"
        )
        conn.execute("INSERT INTO users VALUES (1, 'old', 'Old User', 'o@example.com', 'X', 'agent', 'h', 1)")
        conn.execute(
            "CREATE TABLE tickets (id INTEGER PRIMARY KEY, ticket_no VARCHAR(20), requester_id INTEGER NOT NULL,"
            " department VARCHAR(120) NOT NULL, category VARCHAR(20) NOT NULL, priority VARCHAR(10) NOT NULL,"
            " title VARCHAR(200) NOT NULL, description TEXT NOT NULL, location VARCHAR(200) NOT NULL,"
            " status VARCHAR(20) NOT NULL, assignee_id INTEGER, created_at DATETIME NOT NULL,"
            " updated_at DATETIME NOT NULL, due_at DATETIME NOT NULL, resolved_at DATETIME,"
            " breached BOOLEAN NOT NULL)"
        )
        conn.execute(
            "INSERT INTO tickets VALUES (1, 'FT-000001', 1, 'Ops', 'OTHERS', 'LOW', 't', 'd', 'l', 'ASSIGNED',"
            " 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00', '2026-01-04 00:00:00', NULL, 0)"
        )
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path.as_posix()}"})
    with app.app_context():
        ticket = db.session.get(Ticket, 1)
        assert ticket.is_sos is False and ticket.deleted_at is None
        assert ticket.status == "IN_PROGRESS"
        assert ticket.assigned_team is None  # the old assignee had no team
