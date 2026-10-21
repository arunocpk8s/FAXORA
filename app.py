"""Facility Tracker: app factory and routes."""
import logging
import os
import re
import traceback
from datetime import timedelta

import click
from flask import (
    Blueprint, Flask, Response, abort, current_app, flash, g, jsonify, redirect, render_template,
    request, url_for,
)
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import and_, case, false

import notify
import reports
from admin import bp as admin_bp
from auth import bp as auth_bp
from auth import login_required, role_required
from config import CATEGORIES, DEPARTMENTS, PRIORITIES, Config
from models import (
    FINAL_STATUSES, OPEN_STATUSES, ROLES, STATUSES, TRANSITIONS, Ticket, User, add_event,
    can_transition, db, refresh_breaches, upgrade_schema,
)
from sla import (
    compute_due_at, compute_sos_due_at, format_ist, ist_day_start_utc, ist_month_range_utc,
    ist_month_start_utc, parse_month, recent_months, time_left, utcnow,
)

# A second SOS from the same person within this window opens the existing one instead.
SOS_DUPLICATE_WINDOW = timedelta(minutes=10)
MIN_PASSWORD_LENGTH = 8

csrf = CSRFProtect()
bp = Blueprint("main", __name__)


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be set in the Vercel project's environment variables.")
    try:
        os.makedirs(app.instance_path, exist_ok=True)
    except OSError:
        pass  # read-only file system (e.g. Vercel); only the local SQLite file lives there

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    db.init_app(app)
    csrf.init_app(app)
    app.register_blueprint(auth_bp)
    app.register_blueprint(bp)
    app.register_blueprint(admin_bp)
    app.jinja_env.filters["ist"] = format_ist

    with app.app_context():
        db.create_all()
        upgrade_schema()

    # Company logo: drop logo.svg / logo.png / logo.jpg into static/ to show it in the header.
    logo = next(
        (n for n in ("logo.svg", "logo.png", "logo.jpg") if os.path.exists(os.path.join(app.static_folder, n))),
        None,
    )

    @app.context_processor
    def inject_branding():
        return {
            "app_name": app.config["APP_NAME"],
            "logo_file": logo,
            "emergency_numbers": notify.emergency_numbers(),
            # Hosted copy without a real database: data is lost on restart, so say so on every page.
            "temporary_database": bool(os.environ.get("VERCEL"))
            and app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"),
        }

    register_cli(app)
    return app


def register_cli(app):
    @app.cli.command("resend-failed")
    def resend_failed():
        """Retry sending emails that previously failed."""
        sent, pending = notify.resend_pending()
        click.echo(f"Resent {sent} email(s); {pending} still pending.")

    @app.cli.command("test-email")
    @click.argument("to")
    def test_email(to):
        """Send a test email to TO to check the SMTP settings."""
        error = notify.send_test(to)
        if error:
            raise click.ClickException(f"Sending failed: {error}")
        click.echo(f"Test email sent to {to}.")

    @app.cli.command("set-email")
    @click.argument("username")
    @click.argument("email")
    def set_email(username, email):
        """Change the email address of USERNAME."""
        user = _user_or_fail(username)
        user.email = email.strip()
        db.session.commit()
        click.echo(f"Email for '{username}' set to {user.email}.")

    @app.cli.command("set-team")
    @click.argument("username")
    @click.argument("team", type=click.Choice(CATEGORIES, case_sensitive=False))
    def set_team(username, team):
        """Put agent USERNAME in a facility TEAM."""
        user = _user_or_fail(username)
        if user.role != "agent":
            raise click.ClickException(f"'{username}' is not an agent.")
        user.team = team
        db.session.commit()
        click.echo(f"'{username}' is now in the {team.title()} team.")

    @app.cli.command("set-role")
    @click.argument("username")
    @click.argument("role", type=click.Choice(ROLES))
    def set_role(username, role):
        """Change the role of USERNAME (employee, agent or admin)."""
        user = _user_or_fail(username)
        user.role = role
        if role != "agent":
            user.team = None
        db.session.commit()
        click.echo(f"'{username}' is now {role}.")

    @app.cli.command("create-user")
    @click.argument("username")
    @click.option("--name", "full_name", required=True, help="Full name shown in the app.")
    @click.option("--email", required=True)
    @click.option("--role", type=click.Choice(ROLES), default="employee", show_default=True)
    @click.option("--department", default="", help=f"One of: {', '.join(DEPARTMENTS)}.")
    @click.option("--team", type=click.Choice(CATEGORIES, case_sensitive=False), help="Agents only.")
    @click.password_option()
    def create_user(username, full_name, email, role, department, team, password):
        """Create a user. The password is asked for and never shown."""
        if User.query.filter_by(username=username).first():
            raise click.ClickException(f"User '{username}' already exists.")
        if department and department not in DEPARTMENTS:
            raise click.ClickException(f"Department must be one of: {', '.join(DEPARTMENTS)}.")
        if team and role != "agent":
            raise click.ClickException("Only agents can have a team.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        user = User(
            username=username, full_name=full_name, email=email, role=role,
            department=department, team=team,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"Created {role} '{username}'" + (f" in the {team.title()} team." if team else "."))


def _user_or_fail(username):
    user = User.query.filter_by(username=username).first()
    if user is None:
        raise click.ClickException(f"No user named '{username}'.")
    return user


# --- helpers -----------------------------------------------------------------------------

TICKET_FIELDS = ("department", "category", "priority", "title", "location", "description")

# Dashboard tabs for staff: key -> (label, statuses or None for all).
VIEWS = {
    "open": ("Open", OPEN_STATUSES),
    "done": ("Resolved / closed", ("RESOLVED", "CLOSED")),
    "cancelled": ("Cancelled", ("CANCELLED",)),
    "all": ("All", None),
}


def _read_filters(args):
    category = args.get("category", "").upper()
    status = args.get("status", "").upper()
    team = args.get("team", "").upper()
    month = args.get("month", "")
    view = args.get("view", "all")
    return {
        "view": view if view in VIEWS else "all",
        "category": category if category in CATEGORIES else "",
        "team": team if team in CATEGORIES else "",
        "status": status if status in STATUSES else "",
        "month": month if parse_month(month) else "",
        "breached": args.get("breached") in ("1", "true", "on"),
    }


def _team_scope(query):
    """Admins see every ticket; agents their team's tickets, including unassigned ones in its category."""
    if g.user.is_admin:
        return query
    if not g.user.team:
        return query.filter(false())
    return query.filter(Ticket.team_criteria(g.user.team))


def _filtered_query(filters):
    query = _team_scope(Ticket.live())
    if filters["team"]:
        query = query.filter(Ticket.assigned_team == filters["team"])
    if filters["category"]:
        query = query.filter(Ticket.category == filters["category"])
    if filters["status"]:
        query = query.filter(Ticket.status == filters["status"])
    if filters["breached"]:
        query = query.filter(Ticket.breached.is_(True))
    if filters["month"]:
        start, end = ist_month_range_utc(*parse_month(filters["month"]))
        query = query.filter(Ticket.created_at >= start, Ticket.created_at < end)
    view_statuses = VIEWS[filters["view"]][1]
    if view_statuses:
        query = query.filter(Ticket.status.in_(view_statuses))
    open_sos_first = case((_open_sos_criteria(), 0), else_=1)
    return query.order_by(open_sos_first, Ticket.created_at.desc())


def _open_sos_criteria():
    return and_(Ticket.is_sos.is_(True), Ticket.status.in_(OPEN_STATUSES))


def _counts():
    def count(*criteria):
        return _team_scope(Ticket.live()).filter(*criteria).count()

    return {
        # Every agent sees active SOS, whatever their team.
        "sos": Ticket.live().filter(_open_sos_criteria()).count(),
        "open": count(Ticket.status.in_(OPEN_STATUSES)),
        "overdue": count(Ticket.status.in_(OPEN_STATUSES), Ticket.breached.is_(True)),
        "resolved_today": count(
            Ticket.status.in_(("RESOLVED", "CLOSED")), Ticket.resolved_at >= ist_day_start_utc()
        ),
        "total_month": count(Ticket.created_at >= ist_month_start_utc()),
    }


def _view_tabs(filters):
    """[(key, label, count, url)] for the dashboard tabs; counts follow the other filters."""
    tabs = []
    for key, (label, _statuses) in VIEWS.items():
        tab_filters = dict(filters, view=key, status="")
        args = {k: v for k, v in request.args.items() if k not in ("view", "status")}
        tabs.append((key, label, _filtered_query(tab_filters).count(), url_for("main.dashboard", view=key, **args)))
    return tabs


def _ticket_row(ticket, now):
    if ticket.is_open:
        left, overdue = time_left(ticket.due_at, now)
    else:
        left, overdue = ("Breached" if ticket.breached else "Met"), False
    return {
        "id": ticket.id,
        "ticket_no": ticket.ticket_no,
        "url": url_for("main.ticket_detail", ticket_id=ticket.id),
        "title": ticket.title,
        "category": ticket.category,
        "priority": ticket.priority,
        "status": ticket.status,
        "requester": ticket.requester.full_name,
        "location": ticket.location,
        "is_sos": ticket.is_sos,
        "team": ticket.assigned_team.title() if ticket.assigned_team else "",
        "due_at": format_ist(ticket.due_at),
        "time_left": left,
        "overdue": overdue,
        "breached": ticket.breached,
    }


def _live_ticket_or_404(ticket_id):
    ticket = db.get_or_404(Ticket, ticket_id)
    if ticket.deleted_at is not None:
        abort(404)
    return ticket


def _visible_ticket_or_404(ticket_id):
    ticket = _live_ticket_or_404(ticket_id)
    if not g.user.can_view(ticket):
        abort(404)
    return ticket


def _team_options():
    """[(team, number of active agents)] for the assign-to-team list."""
    agents = User.query.filter_by(role="agent", active=True).all()
    return [(team, sum(1 for a in agents if a.team == team)) for team in CATEGORIES]


def _validate_ticket_form(form):
    errors = {}
    for field in TICKET_FIELDS:
        if not form[field]:
            errors[field] = "This field is required."
    if form["department"] and form["department"] not in DEPARTMENTS:
        errors["department"] = "Choose a valid department."
    if form["category"] and form["category"] not in CATEGORIES:
        errors["category"] = "Choose a valid category."
    if form["priority"] and form["priority"] not in PRIORITIES:
        errors["priority"] = "Choose a valid priority."
    for field, limit in (("title", 200), ("location", 200)):
        if len(form[field]) > limit:
            errors[field] = f"Must be {limit} characters or fewer."
    return errors


def _render_new_ticket(form, errors):
    return render_template(
        "new_ticket.html", form=form, errors=errors, categories=CATEGORIES,
        priorities=PRIORITIES, departments=DEPARTMENTS,
    )


# --- routes ------------------------------------------------------------------------------

@bp.route("/")
@login_required
def dashboard():
    refresh_breaches()
    now = utcnow()
    if g.user.is_staff:
        filters = _read_filters(request.args)
        tickets = _filtered_query(filters).all()
        counts = _counts()
        sos_open = Ticket.live().filter(_open_sos_criteria()).order_by(Ticket.created_at).all()
        views = _view_tabs(filters)
    else:
        filters, counts, sos_open, views = None, None, [], []
        tickets = (
            Ticket.live().filter_by(requester_id=g.user.id).order_by(Ticket.created_at.desc()).all()
        )
    return render_template(
        "dashboard.html",
        rows=[_ticket_row(t, now) for t in tickets],
        sos_open=sos_open,
        views=views,
        counts=counts,
        filters=filters,
        categories=CATEGORIES,
        statuses=STATUSES,
        months=recent_months(),
    )


@bp.route("/tickets/new", methods=["GET", "POST"])
@role_required("employee")
def new_ticket():
    if request.method == "GET":
        form = {f: "" for f in TICKET_FIELDS}
        form.update(
            department=g.user.department if g.user.department in DEPARTMENTS else "",
            priority="NORMAL",
        )
        return _render_new_ticket(form, {})

    form = {f: request.form.get(f, "").strip() for f in TICKET_FIELDS}
    form["category"] = form["category"].upper()
    form["priority"] = form["priority"].upper()
    # Single-line fields end up in email subjects; keep them on one line.
    for field in ("department", "title", "location"):
        form[field] = " ".join(form[field].split())
    errors = _validate_ticket_form(form)
    if errors:
        return _render_new_ticket(form, errors), 400

    now = utcnow()
    ticket = Ticket(
        requester_id=g.user.id,
        status="NEW",
        created_at=now,
        updated_at=now,
        due_at=compute_due_at(now, form["category"], form["priority"]),
        **form,
    )
    db.session.add(ticket)
    db.session.flush()
    ticket.ticket_no = Ticket.format_no(ticket.id)
    add_event(ticket, g.user, "CREATED", new_value="NEW")
    db.session.commit()

    notify.ticket_created(ticket)
    flash(f"Ticket {ticket.ticket_no} created.", "success")
    return redirect(url_for("main.ticket_detail", ticket_id=ticket.id))


@bp.route("/sos", methods=["GET", "POST"])
@login_required
def sos():
    last = (
        Ticket.live().filter_by(requester_id=g.user.id).order_by(Ticket.created_at.desc()).first()
    )
    if request.method == "GET":
        form = {"location": last.location if last else "", "phone": "", "note": ""}
        return render_template("sos.html", form=form, errors={})

    form = {
        "location": " ".join(request.form.get("location", "").split())[:200],
        "phone": " ".join(request.form.get("phone", "").split())[:40],
        "note": request.form.get("note", "").strip()[:2000],
    }
    if not form["location"]:
        return render_template(
            "sos.html", form=form, errors={"location": "Tell us where you are."}
        ), 400

    if last and last.is_sos and last.is_open and utcnow() - last.created_at < SOS_DUPLICATE_WINDOW:
        flash(f"Your SOS {last.ticket_no} is already active. Help has been alerted.", "sos")
        return redirect(url_for("main.ticket_detail", ticket_id=last.id))

    now = utcnow()
    details = [f"Contact number: {form['phone']}" if form["phone"] else "", form["note"]]
    ticket = Ticket(
        requester_id=g.user.id,
        department=g.user.department or "Not specified",
        category="MEDICAL",
        priority="URGENT",
        title="SOS: urgent help needed",
        description="\n\n".join(d for d in details if d) or "SOS raised with no further details.",
        location=form["location"],
        status="NEW",
        is_sos=True,
        assigned_team="MEDICAL",
        created_at=now,
        updated_at=now,
        due_at=compute_sos_due_at(now),
    )
    db.session.add(ticket)
    db.session.flush()
    ticket.ticket_no = Ticket.format_no(ticket.id)
    add_event(ticket, g.user, "CREATED", new_value="NEW", comment="SOS raised")
    db.session.commit()
    current_app.logger.warning("SOS %s raised at %s", ticket.ticket_no, ticket.location)

    notify.sos_raised(ticket, form["phone"])
    flash(f"SOS {ticket.ticket_no} sent. The response team has been alerted.", "sos")
    return redirect(url_for("main.ticket_detail", ticket_id=ticket.id))


@bp.route("/tickets/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id):
    refresh_breaches()
    ticket = _visible_ticket_or_404(ticket_id)
    left = time_left(ticket.due_at)[0] if ticket.is_open else None
    return render_template(
        "ticket_detail.html",
        ticket=ticket,
        # Cancelling has its own form, with a required reason.
        next_statuses=[s for s in TRANSITIONS[ticket.status] if s != "CANCELLED"],
        can_cancel="CANCELLED" in TRANSITIONS[ticket.status],
        can_update=g.user.can_update(ticket),
        team_options=_team_options() if g.user.is_admin else [],
        time_left=left,
    )


@bp.route("/tickets/<int:ticket_id>/status", methods=["POST"])
@role_required("agent", "admin")
def change_status(ticket_id):
    ticket = _live_ticket_or_404(ticket_id)
    if not g.user.can_update(ticket):
        abort(403, description="Only the assigned team or an admin can update this ticket.")
    new_status = request.form.get("status", "").strip().upper()
    comment = request.form.get("comment", "").strip()[:2000] or None

    if new_status not in STATUSES:
        abort(400, description=f"Unknown status '{new_status}'.")
    if not can_transition(ticket.status, new_status):
        allowed = ", ".join(TRANSITIONS[ticket.status]) or "none"
        abort(
            400,
            description=f"Invalid status transition: {ticket.status} -> {new_status}. "
            f"Allowed from {ticket.status}: {allowed}.",
        )
    if new_status == "CANCELLED" and not comment:
        abort(400, description="Give a reason for cancelling the ticket.")

    old_status, now = ticket.status, utcnow()
    # Starting work on an unassigned ticket assigns it to the agent's team (admins: the category team).
    newly_assigned = new_status == "IN_PROGRESS" and ticket.assigned_team is None
    if newly_assigned:
        ticket.assigned_team = g.user.team if g.user.role == "agent" else ticket.category
        add_event(ticket, g.user, "ASSIGNED", None, ticket.assigned_team)
    ticket.status = new_status
    ticket.updated_at = now
    if new_status == "RESOLVED":
        ticket.resolved_at = now
        if now > ticket.due_at:
            ticket.breached = True
    elif old_status == "RESOLVED" and new_status == "IN_PROGRESS":
        ticket.resolved_at = None
    add_event(ticket, g.user, "STATUS_CHANGED", old_status, new_status, comment)
    db.session.commit()

    if newly_assigned:
        notify.team_assigned(ticket)
    notify.status_changed(ticket)
    flash("Ticket cancelled." if new_status == "CANCELLED" else f"Status changed to {new_status}.", "success")
    return redirect(url_for("main.ticket_detail", ticket_id=ticket.id))


@bp.route("/tickets/<int:ticket_id>/assign", methods=["POST"])
@role_required("admin")
def assign(ticket_id):
    ticket = _live_ticket_or_404(ticket_id)
    if ticket.status in FINAL_STATUSES:
        abort(400, description="Closed or cancelled tickets cannot be reassigned.")
    team = request.form.get("team", "").strip().upper()
    if team not in CATEGORIES:
        abort(400, description=f"Choose a team: {', '.join(CATEGORIES)}.")
    if team == ticket.assigned_team:
        flash(f"Already assigned to the {team.title()} team.", "info")
        return redirect(url_for("main.ticket_detail", ticket_id=ticket.id))

    old_team = ticket.assigned_team
    ticket.assigned_team = team
    ticket.updated_at = utcnow()
    add_event(ticket, g.user, "ASSIGNED", old_team, team)
    # Assigning a new ticket starts the work.
    status_moved = ticket.status == "NEW"
    if status_moved:
        ticket.status = "IN_PROGRESS"
        add_event(ticket, g.user, "STATUS_CHANGED", "NEW", "IN_PROGRESS")
    db.session.commit()

    notify.team_assigned(ticket)
    if status_moved:
        notify.status_changed(ticket)
    flash(f"Assigned to the {team.title()} team; they have been emailed.", "success")
    return redirect(url_for("main.ticket_detail", ticket_id=ticket.id))


@bp.route("/tickets/export.csv")
@role_required("agent", "admin")
def export_tickets():
    """The dashboard's current filter (and the user's team scope) as a CSV file for Excel."""
    filters = _read_filters(request.args)
    tickets = _filtered_query(filters).all()
    name = f"faxora-tickets-{filters['month'] or 'all'}.csv"
    current_app.logger.info("%s exported %d tickets", g.user.username, len(tickets))
    return Response(reports.tickets_csv(tickets), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.route("/api/tickets")
@role_required("agent", "admin")
def api_tickets():
    refresh_breaches()
    now = utcnow()
    tickets = _filtered_query(_read_filters(request.args)).all()
    return jsonify(tickets=[_ticket_row(t, now) for t in tickets], counts=_counts())


def safe_create_app():
    """create_app(), or a minimal app that explains why start-up failed (credentials in URLs masked)."""
    try:
        return create_app()
    except Exception as exc:  # noqa: BLE001 - report any start-up failure
        logging.getLogger(__name__).error("FAXORA failed to start:\n%s", traceback.format_exc())
        reason = re.sub(r"://[^/\s@]+@", "://***@", f"{type(exc).__name__}: {exc}")[:500]

        def startup_error(environ, start_response):
            body = (
                "FAXORA could not start on this deployment.\n\n"
                f"Reason: {reason}\n\n"
                "Check the project's Environment Variables in Vercel (SECRET_KEY, DATABASE_URL), "
                "then redeploy.\n"
            ).encode()
            start_response("503 Service Unavailable", [("Content-Type", "text/plain; charset=utf-8"),
                                                       ("Content-Length", str(len(body)))])
            return [body]

        return startup_error

