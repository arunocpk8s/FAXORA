"""Email notifications over SMTP.

Every message is first recorded in the email_outbox table, then sent. Failures are logged and
left as PENDING for `flask resend-failed`; they never break the request that triggered them.
Email bodies and passwords are never written to the log.
"""
import base64
import hashlib
import logging
import smtplib
from email.message import EmailMessage

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from models import EmailOutbox, User, db, get_setting
from sla import format_ist, utcnow

log = logging.getLogger(__name__)

# Mail server settings that the admin console can override (Setting key -> config key).
SMTP_SETTING_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USE_TLS", "SMTP_USER", "SMTP_FROM", "APP_BASE_URL")
SMTP_PASSWORD_KEY = "SMTP_PASSWORD_ENC"


def _fernet():
    """Encryption key for secrets stored in the database, derived from SECRET_KEY."""
    digest = hashlib.sha256(b"faxora-settings:" + current_app.config["SECRET_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value):
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token):
    """Returns None if the token cannot be read (for example after SECRET_KEY changed)."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def smtp_settings():
    """Effective mail server settings: admin console values first, then .env."""
    cfg = current_app.config
    port = get_setting("SMTP_PORT", str(cfg.get("SMTP_PORT") or 25))
    settings = {
        "host": get_setting("SMTP_HOST", cfg.get("SMTP_HOST") or ""),
        "port": int(port) if port.isdigit() else 25,
        "use_tls": get_setting("SMTP_USE_TLS", "true" if cfg.get("SMTP_USE_TLS") else "false") == "true",
        "user": get_setting("SMTP_USER", cfg.get("SMTP_USER") or ""),
        "sender": get_setting("SMTP_FROM", cfg.get("SMTP_FROM") or ""),
        "base_url": get_setting("APP_BASE_URL", cfg.get("APP_BASE_URL") or "").rstrip("/"),
        "timeout": cfg.get("SMTP_TIMEOUT", 10),
    }
    token = get_setting(SMTP_PASSWORD_KEY, None)
    if token is None:
        settings["password"], settings["password_status"] = cfg.get("SMTP_PASSWORD") or "", "env"
    elif token == "":
        settings["password"], settings["password_status"] = "", "none"
    else:
        password = decrypt_secret(token)
        settings["password"] = password or ""
        settings["password_status"] = "saved" if password is not None else "unreadable"
    return settings


def team_mailbox(team):
    """Team mailbox: the admin console setting, else the .env value."""
    default = current_app.config.get("CATEGORY_MAILBOXES", {}).get(team, "")
    return get_setting(f"CATEGORY_MAILBOX_{team}", default)


def sos_alert_emails():
    default = ",".join(current_app.config.get("SOS_ALERT_EMAILS") or [])
    return [e.strip() for e in get_setting("SOS_ALERT_EMAILS", default).split(",") if e.strip()]


def emergency_numbers():
    return get_setting("EMERGENCY_NUMBERS", current_app.config.get("EMERGENCY_NUMBERS", ""))


def _deliver(recipients, subject, body):
    smtp_cfg = smtp_settings()
    if not smtp_cfg["host"]:
        raise RuntimeError("SMTP_HOST is not configured")
    if smtp_cfg["password_status"] == "unreadable":
        raise RuntimeError("Saved SMTP password cannot be read; enter it again in the admin console")
    msg = EmailMessage()
    msg["From"] = smtp_cfg["sender"]
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=smtp_cfg["timeout"]) as smtp:
        if smtp_cfg["use_tls"]:
            smtp.starttls()
        if smtp_cfg["user"]:
            smtp.login(smtp_cfg["user"], smtp_cfg["password"])
        smtp.send_message(msg)


def _attempt(entry):
    entry.attempts += 1
    try:
        _deliver(entry.recipients.split(","), entry.subject, entry.body)
    except Exception as exc:  # noqa: BLE001 - any failure just leaves the email pending
        entry.status = "PENDING"
        entry.last_error = f"{type(exc).__name__}: {exc}"[:500]
        log.warning(
            "Email #%s to %s failed (%s); left pending for retry",
            entry.id, entry.recipients, entry.last_error,
        )
        return False
    entry.status = "SENT"
    entry.sent_at = utcnow()
    entry.last_error = None
    log.info("Email #%s sent to %s (subject: %s)", entry.id, entry.recipients, entry.subject)
    return True


def send(recipients, subject, body, ticket=None):
    """Record an email and try to send it now. Returns True if sent. Never raises."""
    recipients = list(dict.fromkeys(r.strip() for r in recipients if r and r.strip()))
    if not recipients:
        log.info("No recipients for email '%s'; skipped", subject)
        return False
    try:
        entry = EmailOutbox(
            ticket_id=ticket.id if ticket else None,
            recipients=",".join(recipients),
            subject=subject,
            body=body,
            status="PENDING",
            attempts=0,
            created_at=utcnow(),
        )
        db.session.add(entry)
        db.session.flush()
        sent = _attempt(entry)
        db.session.commit()
        return sent
    except Exception:  # noqa: BLE001
        log.exception("Could not record or send email '%s'", subject)
        db.session.rollback()
        return False


def send_test(to):
    """Send a one-off test email (not stored). Returns an error message, or None on success."""
    try:
        _deliver(
            [to],
            f"{current_app.config.get('APP_NAME', 'Facility Tracker')} test email",
            "This is a test email from the Facility Tracker. SMTP settings are working.\n",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Test email to %s failed: %s: %s", to, type(exc).__name__, exc)
        return f"{type(exc).__name__}: {exc}"
    log.info("Test email sent to %s", to)
    return None


def retry(entry):
    """Retry one PENDING email now. Returns True if sent."""
    sent = _attempt(entry)
    db.session.commit()
    return sent


def resend_pending():
    """Retry every PENDING email. Returns (sent, still_pending)."""
    entries = EmailOutbox.query.filter_by(status="PENDING").order_by(EmailOutbox.id).all()
    sent = sum(1 for entry in entries if _attempt(entry))
    db.session.commit()
    return sent, len(entries) - sent


def _ticket_url(ticket):
    base = get_setting("APP_BASE_URL", current_app.config.get("APP_BASE_URL") or "").rstrip("/")
    return f"{base}/tickets/{ticket.id}"


def _body(ticket, intro):
    return (
        f"{intro}\n\n"
        f"Ticket:   {ticket.ticket_no}\n"
        f"Title:    {ticket.title}\n"
        f"Category: {ticket.category}\n"
        f"Priority: {ticket.priority}\n"
        f"Status:   {ticket.status}\n"
        f"Due:      {format_ist(ticket.due_at)}\n\n"
        f"View the ticket: {_ticket_url(ticket)}\n\n"
        "-- Facility Tracker (automated message, please do not reply)\n"
    )


def ticket_created(ticket):
    send(
        [ticket.requester.email],
        f"[{ticket.ticket_no}] Request received: {ticket.title}",
        _body(ticket, "Your facility request has been received."),
        ticket,
    )
    mailbox = team_mailbox(ticket.category)
    send(
        [mailbox],
        f"[{ticket.ticket_no}] New {ticket.category} request: {ticket.title}",
        _body(
            ticket,
            f"A new {ticket.category.lower()} request was raised by {ticket.requester.full_name} "
            f"({ticket.department}) at {ticket.location}.",
        ),
        ticket,
    )


def sos_raised(ticket, phone=""):
    """Alert all agents and admins, the medical mailbox and SOS_ALERT_EMAILS, then confirm to the requester."""
    responders = sos_alert_emails()
    responders.append(team_mailbox("MEDICAL"))
    staff = User.query.filter(User.role.in_(("agent", "admin")), User.active.is_(True))
    responders += [u.email for u in staff]
    contact = f"\nContact number: {phone}" if phone else ""
    send(
        responders,
        f"SOS [{ticket.ticket_no}] Urgent help needed at {ticket.location}",
        _body(
            ticket,
            f"SOS raised by {ticket.requester.full_name} ({ticket.department}).\n"
            f"Location: {ticket.location}{contact}\n"
            "Please respond immediately and update the ticket.",
        ),
        ticket,
    )
    send(
        [ticket.requester.email],
        f"[{ticket.ticket_no}] SOS received",
        _body(
            ticket,
            "Your SOS has been received and the response team has been alerted.\n"
            f"If this is life-threatening, call now: {emergency_numbers()}",
        ),
        ticket,
    )


def status_changed(ticket):
    send(
        [ticket.requester.email],
        f"[{ticket.ticket_no}] Status changed to {ticket.status}",
        _body(ticket, f"The status of your request is now {ticket.status}."),
        ticket,
    )


def team_assigned(ticket):
    """Tell every active agent in the assigned team, and the team mailbox."""
    team = ticket.assigned_team
    members = User.query.filter_by(role="agent", team=team, active=True).order_by(User.id)
    mailbox = team_mailbox(team)
    send(
        [u.email for u in members] + [mailbox],
        f"[{ticket.ticket_no}] Assigned to {team.title()} team: {ticket.title}",
        _body(ticket, f"This ticket has been assigned to the {team.title()} team."),
        ticket,
    )
