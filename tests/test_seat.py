"""Seat numbers (e.g. 1C-154) on requests and SOS."""
import csv
import io

import pytest

from app import normalize_seat
from conftest import login
from models import EmailOutbox, Ticket, db

REQUEST = {
    "title": "Chair broken", "category": "MAINTENANCE", "priority": "NORMAL",
    "department": "DPS", "location": "Block A, 1F", "description": "Wobbly chair",
}


@pytest.mark.parametrize("raw, expected", [
    ("1C-154", "1C-154"), ("1c 154", "1C-154"), ("1c154", "1C-154"), (" 12ab-1234 ", "12AB-1234"),
    ("", ""), ("seat 5", None), ("1-154", None), ("C-154", None), ("1C-15544", None),
])
def test_normalize_seat(raw, expected):
    assert normalize_seat(raw) == expected


def test_request_with_seat_is_saved_shown_and_emailed(app, client):
    login(client, "emp1")
    assert b'placeholder="e.g. 1C-154"' in client.get("/tickets/new").data
    assert client.post("/tickets/new", data=dict(REQUEST, seat_number="1c 154")).status_code == 302
    with app.app_context():
        ticket = Ticket.query.one()
        assert ticket.seat_number == "1C-154"
        bodies = [e.body for e in EmailOutbox.query]
        assert all("Seat:     1C-154" in b for b in bodies)
        assert any("at Block A, 1F (seat 1C-154)" in b for b in bodies)
    assert b"1C-154" in client.get(f"/tickets/{ticket.id}").data
    # The next request starts with the same seat filled in.
    assert b'value="1C-154"' in client.get("/tickets/new").data


def test_seat_is_optional_but_must_look_like_a_seat(app, client):
    login(client, "emp1")
    assert client.post("/tickets/new", data=REQUEST).status_code == 302
    resp = client.post("/tickets/new", data=dict(REQUEST, seat_number="near the window"))
    assert resp.status_code == 400 and b"like 1C-154" in resp.data
    with app.app_context():
        assert [t.seat_number for t in Ticket.query] == [None]


def test_sos_with_seat(app, client):
    login(client, "emp1")
    resp = client.post("/sos", data={"location": "Lobby", "seat_number": "bad seat"})
    assert resp.status_code == 400 and b"like 1C-154" in resp.data
    assert client.post("/sos", data={"location": "Lobby", "seat_number": "2b-17"}).status_code == 302
    with app.app_context():
        alert = EmailOutbox.query.order_by(EmailOutbox.id).first()
        assert alert.subject.endswith("at Lobby (seat 2B-17)")
        assert "Location: Lobby (seat 2B-17)" in alert.body


def test_staff_see_seat_on_dashboard_api_export_and_search(app, client):
    login(client, "emp1")
    client.post("/tickets/new", data=dict(REQUEST, seat_number="1C-154"))
    client.get("/logout")
    login(client, "admin")
    assert client.get("/api/tickets").get_json()["tickets"][0]["seat"] == "1C-154"
    assert b"<th>Seat</th>" in client.get("/").data
    rows = list(csv.reader(io.StringIO(client.get("/tickets/export.csv").data.decode("utf-8-sig"))))
    assert rows[0][10] == "Seat" and rows[1][10] == "1C-154"
    assert b"Chair broken" in client.get("/admin/tickets?q=1C-154").data
    assert b"Chair broken" not in client.get("/admin/tickets?q=9Z-999").data


def test_existing_database_gets_seat_column(app):
    with app.app_context():
        from sqlalchemy import inspect
        assert "seat_number" in {c["name"] for c in inspect(db.engine).get_columns("tickets")}
