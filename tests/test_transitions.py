import pytest

from conftest import login, make_ticket
from models import STATUSES, TRANSITIONS, Ticket, TicketEvent, User, add_event, can_transition, db, upgrade_schema

EXPECTED = {
    ("NEW", "IN_PROGRESS"), ("NEW", "CANCELLED"),
    ("IN_PROGRESS", "ON_HOLD"), ("IN_PROGRESS", "RESOLVED"), ("IN_PROGRESS", "CANCELLED"),
    ("ON_HOLD", "IN_PROGRESS"), ("ON_HOLD", "CANCELLED"),
    ("RESOLVED", "CLOSED"), ("RESOLVED", "IN_PROGRESS"),
}


@pytest.mark.parametrize("old", STATUSES)
@pytest.mark.parametrize("new", STATUSES)
def test_transition_matrix(old, new):
    assert can_transition(old, new) == ((old, new) in EXPECTED)


def change(client, ticket_id, status, comment=""):
    return client.post(f"/tickets/{ticket_id}/status", data={"status": status, "comment": comment})


def test_invalid_transition_returns_400(app, client):
    with app.app_context():
        tid = make_ticket(status="NEW")
    login(client, "admin")
    resp = change(client, tid, "RESOLVED")
    assert resp.status_code == 400
    assert b"Invalid status transition: NEW -&gt; RESOLVED" in resp.data
    with app.app_context():
        assert db.session.get(Ticket, tid).status == "NEW"
        assert TicketEvent.query.filter_by(ticket_id=tid, event_type="STATUS_CHANGED").count() == 0


def test_unknown_status_returns_400(app, client):
    with app.app_context():
        tid = make_ticket()
    login(client, "admin")
    assert change(client, tid, "DONE").status_code == 400


@pytest.mark.parametrize("final", ["CLOSED", "CANCELLED"])
def test_closed_and_cancelled_are_final(app, client, final):
    with app.app_context():
        tid = make_ticket(status=final)
    login(client, "admin")
    for status in STATUSES:
        assert change(client, tid, status, "reason").status_code == 400
    assert client.post(f"/tickets/{tid}/assign", data={"team": "TRANSPORT"}).status_code == 400


def test_starting_unassigned_ticket_assigns_category_team(app, client):
    with app.app_context():
        tid = make_ticket(status="NEW", category="HOUSEKEEPING")
    login(client, "admin")
    assert change(client, tid, "IN_PROGRESS").status_code == 302
    with app.app_context():
        ticket = db.session.get(Ticket, tid)
        assert (ticket.status, ticket.assigned_team) == ("IN_PROGRESS", "HOUSEKEEPING")
        assert [e.event_type for e in ticket.events] == ["CREATED", "ASSIGNED", "STATUS_CHANGED"]


def test_full_lifecycle_writes_events(app, client):
    with app.app_context():
        tid = make_ticket(status="NEW")
    login(client, "admin")

    resp = client.post(f"/tickets/{tid}/assign", data={"team": "TRANSPORT"})
    assert resp.status_code == 302
    with app.app_context():
        ticket = db.session.get(Ticket, tid)
        assert ticket.status == "IN_PROGRESS" and ticket.assigned_team == "TRANSPORT"

    for status in ("ON_HOLD", "IN_PROGRESS", "RESOLVED"):
        assert change(client, tid, status, comment=f"to {status}").status_code == 302
    with app.app_context():
        assert db.session.get(Ticket, tid).resolved_at is not None

    assert change(client, tid, "IN_PROGRESS", "reopened").status_code == 302
    with app.app_context():
        assert db.session.get(Ticket, tid).resolved_at is None

    assert change(client, tid, "RESOLVED").status_code == 302
    assert change(client, tid, "CLOSED").status_code == 302

    with app.app_context():
        events = TicketEvent.query.filter_by(ticket_id=tid).order_by(TicketEvent.id).all()
        types = [e.event_type for e in events]
        assert types == ["CREATED", "ASSIGNED"] + ["STATUS_CHANGED"] * 7
        changes = [(e.old_value, e.new_value) for e in events if e.event_type == "STATUS_CHANGED"]
        assert all(pair in EXPECTED for pair in changes)
        assert (events[2].old_value, events[2].new_value) == ("NEW", "IN_PROGRESS")
        assert events[3].comment == "to ON_HOLD"


@pytest.mark.parametrize("start", ["NEW", "IN_PROGRESS", "ON_HOLD"])
def test_cancel_open_ticket_needs_reason(app, client, start):
    with app.app_context():
        tid = make_ticket(status=start, team="MAINTENANCE")
    login(client, "admin")
    resp = change(client, tid, "CANCELLED")
    assert resp.status_code == 400 and b"reason for cancelling" in resp.data
    assert change(client, tid, "CANCELLED", "duplicate").status_code == 302
    with app.app_context():
        ticket = db.session.get(Ticket, tid)
        assert ticket.status == "CANCELLED" and ticket.deleted_at is None
        assert ticket.events[-1].comment == "duplicate"


def test_resolved_ticket_cannot_be_cancelled(app, client):
    with app.app_context():
        tid = make_ticket(status="RESOLVED", team="MAINTENANCE")
    login(client, "admin")
    assert change(client, tid, "CANCELLED", "too late").status_code == 400


def test_old_cancelled_tickets_are_migrated(app):
    """Before CANCELLED existed, cancelling was NEW -> CLOSED."""
    with app.app_context():
        cancelled = make_ticket(status="CLOSED")
        resolved = make_ticket(status="CLOSED")
        admin = User.query.filter_by(username="admin").one()
        add_event(db.session.get(Ticket, cancelled), admin, "STATUS_CHANGED", "NEW", "CLOSED")
        add_event(db.session.get(Ticket, resolved), admin, "STATUS_CHANGED", "RESOLVED", "CLOSED")
        db.session.commit()
        upgrade_schema()
        db.session.expire_all()
        assert db.session.get(Ticket, cancelled).status == "CANCELLED"
        assert db.session.get(Ticket, resolved).status == "CLOSED"


def test_events_are_append_only(app):
    with app.app_context():
        make_ticket()
        event = TicketEvent.query.first()
        event.comment = "tampered"
        with pytest.raises(RuntimeError):
            db.session.flush()
        db.session.rollback()
        db.session.delete(TicketEvent.query.first())
        with pytest.raises(RuntimeError):
            db.session.flush()


def test_transitions_table_covers_all_statuses():
    assert set(TRANSITIONS) == set(STATUSES)
