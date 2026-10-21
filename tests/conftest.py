import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import create_app  # noqa: E402
from models import Ticket, User, add_event, db  # noqa: E402
from sla import compute_due_at, utcnow  # noqa: E402

PASSWORD = "test-password-123"


@pytest.fixture
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
            "WTF_CSRF_ENABLED": False,
            "SMTP_HOST": "",
            "APP_BASE_URL": "http://tracker.test",
            "SOS_ALERT_EMAILS": ["security@example.com"],
            "CATEGORY_MAILBOXES": {
                "TRANSPORT": "transport@example.com",
                "SECURITY": "security@example.com",
                "HOUSEKEEPING": "housekeeping@example.com",
                "MAINTENANCE": "maintenance@example.com",
                "MEDICAL": "medical@example.com",
                "OTHERS": "facilities@example.com",
            },
        }
    )
    with app.app_context():
        for username, role, team in (
            ("admin", "admin", None),
            ("agent", "agent", "HOUSEKEEPING"), ("agent2", "agent", "TRANSPORT"),
            ("emp1", "employee", None), ("emp2", "employee", None),
        ):
            user = User(
                username=username, full_name=username.title(), role=role, team=team,
                department="DPS", email=f"{username}@example.com",
            )
            user.set_password(PASSWORD)
            db.session.add(user)
        db.session.commit()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


def login(client, username):
    return client.post("/login", data={"username": username, "password": PASSWORD})


def user_id(username):
    return User.query.filter_by(username=username).one().id


def make_ticket(requester="emp1", status="NEW", category="MAINTENANCE", priority="NORMAL",
                team=None, created_at=None):
    """Insert a ticket directly (call inside an app context). Returns its id."""
    created_at = created_at or utcnow()
    requester_user = User.query.filter_by(username=requester).one()
    ticket = Ticket(
        requester_id=requester_user.id,
        department="Ops",
        category=category,
        priority=priority,
        title="Leaking tap",
        description="Secret details that must not be emailed",
        location="Block A, 2F",
        status=status,
        assigned_team=team,
        created_at=created_at,
        updated_at=created_at,
        due_at=compute_due_at(created_at, category, priority),
        resolved_at=created_at + timedelta(hours=1) if status in ("RESOLVED", "CLOSED") else None,
    )
    db.session.add(ticket)
    db.session.flush()
    ticket.ticket_no = Ticket.format_no(ticket.id)
    add_event(ticket, requester_user, "CREATED", new_value="NEW")
    db.session.commit()
    return ticket.id
