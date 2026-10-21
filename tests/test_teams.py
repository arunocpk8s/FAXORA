"""Team agents see and update only tickets assigned to their team."""
from conftest import login, make_ticket
from models import Ticket, db


def test_agent_updates_own_team_ticket(app, client):
    with app.app_context():
        tid = make_ticket(status="IN_PROGRESS", team="HOUSEKEEPING")
    login(client, "agent")  # Housekeeping team
    page = client.get(f"/tickets/{tid}").data
    assert b"Change status" in page and b"Assign to facility team" not in page
    assert client.post(f"/tickets/{tid}/status", data={"status": "RESOLVED"}).status_code == 302
    with app.app_context():
        assert db.session.get(Ticket, tid).status == "RESOLVED"


def test_agent_cannot_touch_other_team_ticket(app, client):
    with app.app_context():
        tid = make_ticket(status="IN_PROGRESS", team="TRANSPORT")
    login(client, "agent")
    assert client.get(f"/tickets/{tid}").status_code == 404
    assert client.post(f"/tickets/{tid}/status", data={"status": "RESOLVED"}).status_code == 403
    assert client.post(f"/tickets/{tid}/assign", data={"team": "HOUSEKEEPING"}).status_code == 403


def test_agent_dashboard_scoped_to_team(app, client):
    with app.app_context():
        make_ticket(team="HOUSEKEEPING")
        make_ticket(team="TRANSPORT")
        make_ticket()  # not assigned yet
    login(client, "agent")
    data = client.get("/api/tickets").get_json()
    assert [t["team"] for t in data["tickets"]] == ["Housekeeping"]
    assert data["counts"]["open"] == 1
    assert b"Housekeeping team tickets" in client.get("/").data


def test_admin_sees_everything(app, client):
    with app.app_context():
        make_ticket(team="HOUSEKEEPING")
        make_ticket(team="TRANSPORT")
        make_ticket()
    login(client, "admin")
    assert len(client.get("/api/tickets").get_json()["tickets"]) == 3
    assert len(client.get("/api/tickets?team=TRANSPORT").get_json()["tickets"]) == 1


def test_agent_without_team_sees_nothing(app, client):
    app.test_cli_runner().invoke(
        args=["create-user", "lone", "--name", "Lone", "--email", "l@example.com", "--role", "agent"],
        input="long-password\nlong-password\n",
    )
    with app.app_context():
        make_ticket(team="HOUSEKEEPING")
    client.post("/login", data={"username": "lone", "password": "long-password"})
    assert client.get("/api/tickets").get_json()["tickets"] == []
    assert b"not in a team yet" in client.get("/").data


def test_every_agent_can_open_sos(app, client):
    login(client, "emp1")
    client.post("/sos", data={"location": "Lobby"})
    client.get("/logout")
    with app.app_context():
        tid = Ticket.query.one().id
    login(client, "agent2")  # Transport, not Medical
    assert b"1 active SOS" in client.get("/").data
    assert client.get(f"/tickets/{tid}").status_code == 200
    assert client.post(f"/tickets/{tid}/status", data={"status": "CLOSED"}).status_code == 403
