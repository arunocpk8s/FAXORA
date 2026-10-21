"""Database models: User, Ticket, TicketEvent (history), EmailOutbox, Setting and AdminEvent."""
import sqlalchemy as sa
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from werkzeug.security import check_password_hash, generate_password_hash

from sla import utcnow

db = SQLAlchemy()

# employee: raises requests. agent: member of a facility team, updates that team's tickets.
# admin: sees everything, assigns tickets to teams, deletes tickets.
ROLES = ("employee", "agent", "admin")
# Roles the admin console creates; employees will sign in with company SSO.
STAFF_ROLES = ("agent", "admin")
STATUSES = ("NEW", "IN_PROGRESS", "ON_HOLD", "RESOLVED", "CLOSED", "CANCELLED")
OPEN_STATUSES = ("NEW", "IN_PROGRESS", "ON_HOLD")
FINAL_STATUSES = ("CLOSED", "CANCELLED")

# Allowed status transitions, enforced on the server.
# Assigning a NEW ticket to a team moves it to IN_PROGRESS. Open tickets can be cancelled (with a reason).
TRANSITIONS = {
    "NEW": ("IN_PROGRESS", "CANCELLED"),
    "IN_PROGRESS": ("ON_HOLD", "RESOLVED", "CANCELLED"),
    "ON_HOLD": ("IN_PROGRESS", "CANCELLED"),
    "RESOLVED": ("CLOSED", "IN_PROGRESS"),
    "CLOSED": (),
    "CANCELLED": (),
}


def can_transition(old_status, new_status):
    return new_status in TRANSITIONS.get(old_status, ())


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), nullable=False)
    department = db.Column(db.String(120), nullable=False, default="")
    role = db.Column(db.String(20), nullable=False, default="employee")
    # Agents only: the facility team they belong to (one of config.CATEGORIES).
    team = db.Column(db.String(20))
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_staff(self):
        """Agents and admins: the people who handle tickets."""
        return self.role in ("agent", "admin")

    @property
    def is_admin(self):
        return self.role == "admin"

    def can_update(self, ticket):
        """Admins update any ticket; agents only their team's tickets (see Ticket.belongs_to)."""
        if self.is_admin:
            return True
        return self.role == "agent" and self.team is not None and ticket.belongs_to(self.team)

    def can_view(self, ticket):
        if self.role == "employee":
            return ticket.requester_id == self.id
        # Every agent can open an SOS, so anyone can respond in an emergency.
        return self.can_update(ticket) or ticket.is_sos


class Ticket(db.Model):
    __tablename__ = "tickets"

    id = db.Column(db.Integer, primary_key=True)
    ticket_no = db.Column(db.String(20), unique=True)
    requester_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    department = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(20), nullable=False, index=True)
    priority = db.Column(db.String(10), nullable=False, default="NORMAL")
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    location = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="NEW", index=True)
    # Facility team handling the ticket (one of config.CATEGORIES); set by an admin.
    assigned_team = db.Column(db.String(20), index=True)
    # No longer used (tickets were once assigned to a person); kept so old databases still match.
    assignee_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    due_at = db.Column(db.DateTime, nullable=False)
    resolved_at = db.Column(db.DateTime)
    breached = db.Column(db.Boolean, nullable=False, default=False)
    is_sos = db.Column(db.Boolean, nullable=False, default=False, server_default=sa.false())
    # Deleted tickets are hidden everywhere but kept, with their history, for audit.
    deleted_at = db.Column(db.DateTime)
    deleted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    requester = db.relationship("User", foreign_keys=[requester_id])
    events = db.relationship(
        "TicketEvent", order_by="TicketEvent.id", back_populates="ticket", passive_deletes="all"
    )

    def belongs_to(self, team):
        """Assigned to the team, or not yet assigned and in the team's category."""
        if self.assigned_team is not None:
            return self.assigned_team == team
        return self.category == team

    @classmethod
    def team_criteria(cls, team):
        """SQL form of belongs_to()."""
        return sa.or_(
            cls.assigned_team == team,
            sa.and_(cls.assigned_team.is_(None), cls.category == team),
        )

    @classmethod
    def live(cls):
        """Query of tickets that have not been deleted."""
        return cls.query.filter(cls.deleted_at.is_(None))

    @property
    def is_open(self):
        return self.status in OPEN_STATUSES

    @staticmethod
    def format_no(ticket_id):
        return f"FT-{ticket_id:06d}"


class TicketEvent(db.Model):
    """Append-only history. Rows are never updated or deleted by the application."""

    __tablename__ = "ticket_events"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=False, index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    event_type = db.Column(db.String(30), nullable=False)
    old_value = db.Column(db.String(200))
    new_value = db.Column(db.String(200))
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    ticket = db.relationship("Ticket", back_populates="events")
    actor = db.relationship("User")


@event.listens_for(TicketEvent, "before_update")
@event.listens_for(TicketEvent, "before_delete")
def _events_are_append_only(mapper, connection, target):
    raise RuntimeError("TicketEvent rows are append-only")


class EmailOutbox(db.Model):
    """Every email attempt. PENDING rows are retried by `flask resend-failed`."""

    __tablename__ = "email_outbox"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"))
    recipients = db.Column(db.String(500), nullable=False)
    subject = db.Column(db.String(300), nullable=False)
    body = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(10), nullable=False, default="PENDING", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    sent_at = db.Column(db.DateTime)


class Setting(db.Model):
    """Settings changed in the admin console; they override the .env defaults."""

    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))


class AdminEvent(db.Model):
    """Append-only log of admin console actions. Never contains passwords."""

    __tablename__ = "admin_events"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    action = db.Column(db.String(40), nullable=False)
    target = db.Column(db.String(200), nullable=False, default="")
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    actor = db.relationship("User")


@event.listens_for(AdminEvent, "before_update")
@event.listens_for(AdminEvent, "before_delete")
def _admin_events_are_append_only(mapper, connection, target):
    raise RuntimeError("AdminEvent rows are append-only")


def get_setting(key, default=""):
    row = db.session.get(Setting, key)
    return row.value if row is not None else default


def delete_setting(key):
    row = db.session.get(Setting, key)
    if row is not None:
        db.session.delete(row)


def set_setting(key, value, actor):
    row = db.session.get(Setting, key) or Setting(key=key)
    row.value = value
    row.updated_at = utcnow()
    row.updated_by_id = actor.id
    db.session.add(row)


def add_admin_event(actor, action, target="", detail=None):
    db.session.add(AdminEvent(actor_id=actor.id, action=action, target=target, detail=detail, created_at=utcnow()))


def add_event(ticket, actor, event_type, old_value=None, new_value=None, comment=None):
    db.session.add(
        TicketEvent(
            ticket=ticket,
            actor_id=actor.id,
            event_type=event_type,
            old_value=old_value,
            new_value=new_value,
            comment=comment,
            created_at=utcnow(),
        )
    )


# Columns added after the first release: (table, column, SQL definition).
ADDED_COLUMNS = [
    ("tickets", "is_sos", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("tickets", "deleted_at", "TIMESTAMP"),
    ("tickets", "deleted_by_id", "INTEGER REFERENCES users(id)"),
    ("users", "team", "VARCHAR(20)"),
    ("tickets", "assigned_team", "VARCHAR(20)"),
]


def upgrade_schema():
    """Bring an existing database up to date with the current models."""
    inspector = sa.inspect(db.engine)
    existing = {t: {c["name"] for c in inspector.get_columns(t)} for t in ("tickets", "users")}
    with db.engine.begin() as conn:
        for table, column, definition in ADDED_COLUMNS:
            if column not in existing[table]:
                conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
        # The ASSIGNED status was removed: assigned tickets are now IN_PROGRESS.
        conn.execute(sa.text("UPDATE tickets SET status = 'IN_PROGRESS' WHERE status = 'ASSIGNED'"))
        # Tickets used to be assigned to a person: carry that person's team over.
        conn.execute(sa.text(
            "UPDATE tickets SET assigned_team = "
            "(SELECT users.team FROM users WHERE users.id = tickets.assignee_id) "
            "WHERE assigned_team IS NULL AND assignee_id IS NOT NULL"
        ))
        # Cancelling used to be NEW -> CLOSED; those tickets are now CANCELLED.
        conn.execute(sa.text(
            "UPDATE tickets SET status = 'CANCELLED' WHERE status = 'CLOSED' AND id IN "
            "(SELECT ticket_id FROM ticket_events WHERE event_type = 'STATUS_CHANGED' "
            "AND old_value = 'NEW' AND new_value = 'CLOSED')"
        ))


def refresh_breaches():
    """Flag open tickets that have passed their due time. Called on read."""
    count = (
        Ticket.query.filter(
            Ticket.breached.is_(False),
            Ticket.status.in_(OPEN_STATUSES),
            Ticket.due_at < utcnow(),
        ).update({"breached": True}, synchronize_session=False)
    )
    if count:
        db.session.commit()
    return count
