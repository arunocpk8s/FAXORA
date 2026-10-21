"""Login, logout and access-control decorators."""
import logging
from functools import wraps

from flask import (
    Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session, url_for,
)

from models import User, db

log = logging.getLogger(__name__)
bp = Blueprint("auth", __name__)


@bp.before_app_request
def load_current_user():
    user_id = session.get("user_id")
    g.user = db.session.get(User, user_id) if user_id else None
    if g.user is not None and not g.user.active:
        session.clear()
        g.user = None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Login required"), 401
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def _safe_next(target):
    # Only allow redirects to local paths.
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("main.dashboard")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user and user.active and user.check_password(password):
            session.clear()
            session["user_id"] = user.id
            log.info("Login succeeded for user '%s'", username)
            return redirect(_safe_next(request.form.get("next")))
        log.info("Login failed for user '%s'", username)
        flash("Invalid username or password.", "error")
        return render_template("login.html", username=username, next=request.form.get("next", "")), 401
    if g.user:
        return redirect(url_for("main.dashboard"))
    return render_template("login.html", username="", next=request.args.get("next", ""))


@bp.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
