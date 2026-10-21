"""Admin console: access, user management, mail settings and the email outbox."""
from conftest import PASSWORD, login, make_ticket, user_id
from models import AdminEvent, EmailOutbox, User, db

NEW_USER = {
    "username": "ravi.k", "full_name": "Ravi Kumar", "email": "ravi@example.com",
    "role": "agent", "team": "TRANSPORT",
    "password": "new-password-1", "password2": "new-password-1",
}


def actions(app):
    with app.app_context():
        return [e.action for e in AdminEvent.query.order_by(AdminEvent.id)]


# --- access ------------------------------------------------------------------------------------
def test_console_is_admin_only(client):
    assert client.get("/admin/").status_code == 302
    for username in ("emp1", "agent"):
        login(client, username)
        assert client.get("/admin/").status_code == 403
        assert client.get("/admin/users").status_code == 403
        assert client.post("/admin/users/new", data=NEW_USER).status_code == 403
        assert client.get("/admin/mail").status_code == 403
        assert client.get("/admin/outbox").status_code == 403
        client.get("/logout")
    login(client, "admin")
    for path in ("/admin/", "/admin/users", "/admin/users/new", "/admin/mail", "/admin/outbox"):
        assert client.get(path).status_code == 200, path
    assert b"Admin console" in client.get("/").data


def test_csrf_enforced_on_console(app):
    app.config["WTF_CSRF_ENABLED"] = True
    client = app.test_client()
    with client.session_transaction() as s:
        with app.app_context():
            s["user_id"] = user_id("admin")
    assert client.post("/admin/users/new", data=NEW_USER).status_code == 400


# --- users -------------------------------------------------------------------------------------
def test_create_user_and_log_in(app, client):
    login(client, "admin")
    assert client.post("/admin/users/new", data=NEW_USER).status_code == 302
    with app.app_context():
        user = User.query.filter_by(username="ravi.k").one()
        assert (user.role, user.team, user.department, user.active) == ("agent", "TRANSPORT", "", True)
    assert actions(app) == ["USER_CREATED"]
    client.get("/logout")
    assert client.post("/login", data={"username": "ravi.k", "password": "new-password-1"}).status_code == 302


def test_create_user_validation(app, client):
    login(client, "admin")
    cases = [
        (dict(NEW_USER, username="emp1"), b"already taken"),
        (dict(NEW_USER, username="EMP1"), b"already taken"),
        (dict(NEW_USER, username="a b"), b"letters, numbers"),
        (dict(NEW_USER, email="not-an-email"), b"valid email"),
        (dict(NEW_USER, team=""), b"Agents need a team"),
        (dict(NEW_USER, password="short", password2="short"), b"at least 8"),
        (dict(NEW_USER, password2="different-pass"), b"do not match"),
        (dict(NEW_USER, role="superuser"), b"Choose Agent or Admin"),
        (dict(NEW_USER, role="employee"), b"Choose Agent or Admin"),
    ]
    for data, message in cases:
        resp = client.post("/admin/users/new", data=data)
        assert resp.status_code == 400 and message in resp.data, data
    with app.app_context():
        assert User.query.filter_by(username="ravi.k").first() is None


def test_new_user_form_is_staff_only(client):
    login(client, "admin")
    page = client.get("/admin/users/new").data
    assert b'<option value="agent"' in page and b'<option value="admin"' in page
    assert b'<option value="employee"' not in page and b'name="department"' not in page


def test_admin_gets_no_team(app, client):
    login(client, "admin")
    client.post("/admin/users/new", data=dict(NEW_USER, role="admin"))
    with app.app_context():
        assert User.query.filter_by(username="ravi.k").one().team is None


def test_edit_user(app, client):
    with app.app_context():
        uid = user_id("agent")
    login(client, "admin")
    resp = client.post(f"/admin/users/{uid}", data={
        "full_name": "Agent Renamed", "email": "renamed@example.com", "role": "agent", "team": "SECURITY",
    })
    assert resp.status_code == 302
    with app.app_context():
        user = db.session.get(User, uid)
        assert (user.full_name, user.email, user.team, user.department) == ("Agent Renamed", "renamed@example.com", "SECURITY", "DPS")
        event = AdminEvent.query.filter_by(action="USER_UPDATED").one()
        assert "team: HOUSEKEEPING -> SECURITY" in event.detail
    # An agent cannot be turned into an employee here; making them admin clears the team.
    resp = client.post(f"/admin/users/{uid}", data={"full_name": "A", "email": "a@example.com", "role": "employee"})
    assert resp.status_code == 400
    client.post(f"/admin/users/{uid}", data={"full_name": "A", "email": "a@example.com", "role": "admin", "team": "SECURITY"})
    with app.app_context():
        assert (db.session.get(User, uid).role, db.session.get(User, uid).team) == ("admin", None)


def test_existing_employee_can_still_be_edited(app, client):
    with app.app_context():
        uid = user_id("emp1")
    login(client, "admin")
    page = client.get(f"/admin/users/{uid}").data
    assert b'<option value="employee" selected' in page
    resp = client.post(f"/admin/users/{uid}", data={"full_name": "Emp One", "email": "e1@example.com", "role": "employee"})
    assert resp.status_code == 302
    with app.app_context():
        assert db.session.get(User, uid).full_name == "Emp One"


def test_reset_password(app, client):
    with app.app_context():
        uid = user_id("emp1")
    login(client, "admin")
    client.post(f"/admin/users/{uid}/password", data={"password": "short", "password2": "short"})
    client.post(f"/admin/users/{uid}/password", data={"password": "brand-new-pass", "password2": "brand-new-pass"})
    assert actions(app) == ["PASSWORD_RESET"]
    with app.app_context():
        detail = AdminEvent.query.one().detail or ""
        assert "brand-new-pass" not in detail
    client.get("/logout")
    assert client.post("/login", data={"username": "emp1", "password": PASSWORD}).status_code == 401
    assert client.post("/login", data={"username": "emp1", "password": "brand-new-pass"}).status_code == 302


def test_deactivate_and_reactivate(app, client):
    with app.app_context():
        uid = user_id("emp1")
    emp = app.test_client()
    login(emp, "emp1")
    assert emp.get("/").status_code == 200
    login(client, "admin")
    client.post(f"/admin/users/{uid}/active", data={"active": "0"})
    # Signed out at once and cannot log in again.
    assert emp.get("/").status_code == 302
    assert login(emp, "emp1").status_code == 401
    client.post(f"/admin/users/{uid}/active", data={"active": "1"})
    assert login(emp, "emp1").status_code == 302
    assert actions(app) == ["USER_DEACTIVATED", "USER_ACTIVATED"]


def test_admin_cannot_lock_themselves_out(app, client):
    with app.app_context():
        admin_id = user_id("admin")
    login(client, "admin")
    resp = client.post(f"/admin/users/{admin_id}/active", data={"active": "0"}, follow_redirects=True)
    assert b"cannot deactivate your own account" in resp.data
    resp = client.post(f"/admin/users/{admin_id}", data={"full_name": "Admin", "email": "admin@example.com",
                                                          "role": "agent", "team": "OTHERS"})
    assert resp.status_code == 400 and b"your own admin role" in resp.data
    with app.app_context():
        admin = db.session.get(User, admin_id)
        assert admin.active and admin.role == "admin"
    # Another admin can be deactivated while this one stays active.
    client.post("/admin/users/new", data=dict(NEW_USER, username="admin2", role="admin", team=""))
    with app.app_context():
        admin2 = user_id("admin2")
    client.post(f"/admin/users/{admin2}/active", data={"active": "0"})
    with app.app_context():
        assert db.session.get(User, admin2).active is False


def test_last_active_admin_cannot_be_demoted_by_other_admin(app, client):
    login(client, "admin")
    client.post("/admin/users/new", data=dict(NEW_USER, username="admin2", role="admin", team=""))
    with app.app_context():
        admin_id = user_id("admin")
        db.session.get(User, admin_id).active = False  # admin2 is now the only active admin
        db.session.commit()
        admin2 = user_id("admin2")
    other = app.test_client()
    other.post("/login", data={"username": "admin2", "password": NEW_USER["password"]})
    with app.app_context():
        db.session.get(User, admin_id).active = True
        db.session.commit()
    # Two active admins: admin2 may demote admin.
    resp = other.post(f"/admin/users/{admin_id}", data={"full_name": "Admin", "email": "admin@example.com",
                                                         "role": "agent", "team": "OTHERS"})
    assert resp.status_code == 302
    # Now admin2 is the last active admin and cannot demote or deactivate themself.
    resp = other.post(f"/admin/users/{admin2}", data={"full_name": "A2", "email": "a2@example.com", "role": "agent", "team": "OTHERS"})
    assert resp.status_code == 400


def test_user_list_filters(client):
    login(client, "admin")
    page = client.get("/admin/users?role=agent").data
    assert b"agent2" in page and b"emp1" not in page
    page = client.get("/admin/users?q=emp2").data
    assert b"emp2" in page and b"agent2" not in page


# --- mail settings -----------------------------------------------------------------------------
MAIL_FORM = {
    "CATEGORY_MAILBOX_TRANSPORT": "fleet@example.com",
    "CATEGORY_MAILBOX_SECURITY": "security@example.com",
    "CATEGORY_MAILBOX_HOUSEKEEPING": "",
    "CATEGORY_MAILBOX_MAINTENANCE": "maintenance@example.com",
    "CATEGORY_MAILBOX_MEDICAL": "clinic@example.com",
    "CATEGORY_MAILBOX_OTHERS": "facilities@example.com",
    "SOS_ALERT_EMAILS": "guard@example.com; reception@example.com",
    "EMERGENCY_NUMBERS": "Emergency 112 · Site doctor ext. 555",
}


def test_mail_settings_override_env(app, client):
    login(client, "admin")
    assert client.post("/admin/mail", data=MAIL_FORM).status_code == 302
    assert actions(app) == ["MAIL_SETTINGS_UPDATED"]
    page = client.get("/admin/mail").data
    assert b"fleet@example.com" in page and b"guard@example.com, reception@example.com" in page
    # Saving the same values again records nothing new.
    client.post("/admin/mail", data=MAIL_FORM)
    assert actions(app) == ["MAIL_SETTINGS_UPDATED"]

    client.get("/logout")
    login(client, "emp1")
    client.post("/tickets/new", data={"title": "Cab", "category": "TRANSPORT", "priority": "NORMAL",
                                      "department": "DPS", "location": "Gate", "description": "x"})
    client.post("/sos", data={"location": "Lobby"})
    assert b"Site doctor ext. 555" in client.get("/sos").data
    with app.app_context():
        recipients = [e.recipients for e in EmailOutbox.query.order_by(EmailOutbox.id)]
    assert "fleet@example.com" in recipients
    sos_alert = recipients[2].split(",")
    assert "guard@example.com" in sos_alert and "clinic@example.com" in sos_alert
    assert "medical@example.com" not in sos_alert


def test_empty_mailbox_means_no_team_email(app, client):
    login(client, "admin")
    client.post("/admin/mail", data=MAIL_FORM)  # Housekeeping mailbox emptied
    with app.app_context():
        tid = make_ticket(category="HOUSEKEEPING")
        before = EmailOutbox.query.count()
    client.post(f"/tickets/{tid}/assign", data={"team": "HOUSEKEEPING"})
    with app.app_context():
        team_mail = EmailOutbox.query.order_by(EmailOutbox.id).all()[before]
        assert team_mail.recipients == "agent@example.com"


def test_mail_settings_validation(app, client):
    login(client, "admin")
    resp = client.post("/admin/mail", data=dict(MAIL_FORM, CATEGORY_MAILBOX_TRANSPORT="nope",
                                                SOS_ALERT_EMAILS="ok@example.com, bad", EMERGENCY_NUMBERS=""))
    assert resp.status_code == 400
    assert b"one valid email" in resp.data and b"Not valid: bad" in resp.data and b"Enter the emergency numbers" in resp.data
    assert actions(app) == []


def test_mail_page_never_shows_smtp_password(app, client):
    app.config["SMTP_PASSWORD"] = "super-secret-smtp"
    login(client, "admin")
    page = client.get("/admin/mail").data
    assert b"super-secret-smtp" not in page and b"using the password from .env" in page


def test_send_test_email(app, client, monkeypatch):
    login(client, "admin")
    resp = client.post("/admin/mail/test", data={"to": "me@example.com"}, follow_redirects=True)
    assert b"Test email failed" in resp.data
    sent = []
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: sent.append(to))
    resp = client.post("/admin/mail/test", data={"to": "me@example.com"}, follow_redirects=True)
    assert b"Test email sent" in resp.data and sent == [["me@example.com"]]
    assert actions(app) == ["TEST_EMAIL", "TEST_EMAIL"]


# --- outbox ------------------------------------------------------------------------------------
def _pending_emails(app, client):
    login(client, "emp1")
    client.post("/sos", data={"location": "Lobby"})  # 2 PENDING emails (no SMTP in tests)
    client.get("/logout")
    login(client, "admin")
    with app.app_context():
        return [e.id for e in EmailOutbox.query.order_by(EmailOutbox.id)]


def test_outbox_retry_and_cancel(app, client, monkeypatch):
    first, second = _pending_emails(app, client)
    page = client.get("/admin/outbox?status=PENDING").data
    assert b"Retry" in page and b"Resend all 2 pending" in page
    client.post(f"/admin/outbox/{second}/cancel")
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: None)
    client.post(f"/admin/outbox/{first}/retry")
    with app.app_context():
        assert db.session.get(EmailOutbox, first).status == "SENT"
        assert db.session.get(EmailOutbox, second).status == "CANCELLED"
    # Only pending emails can be retried or cancelled.
    assert client.post(f"/admin/outbox/{first}/retry").status_code == 400
    assert client.post(f"/admin/outbox/{second}/cancel").status_code == 400
    assert actions(app) == ["EMAIL_CANCELLED", "EMAIL_RETRIED"]


def test_outbox_resend_all(app, client, monkeypatch):
    _pending_emails(app, client)
    monkeypatch.setattr("notify._deliver", lambda to, subject, body: None)
    resp = client.post("/admin/outbox/resend-all", follow_redirects=True)
    assert b"Resent 2 email(s); 0 still pending." in resp.data
    with app.app_context():
        assert {e.status for e in EmailOutbox.query} == {"SENT"}


def test_admin_events_are_append_only(app, client):
    login(client, "admin")
    client.post("/admin/users/new", data=NEW_USER)
    with app.app_context():
        event = AdminEvent.query.one()
        event.detail = "tampered"
        try:
            db.session.flush()
        except RuntimeError:
            db.session.rollback()
        else:
            raise AssertionError("admin events must be append-only")
