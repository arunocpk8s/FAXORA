"""Configuration read from environment variables (and an optional .env file)."""
import logging
import os
import secrets

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

CATEGORIES = ("TRANSPORT", "SECURITY", "HOUSEKEEPING", "MAINTENANCE", "MEDICAL", "OTHERS")
PRIORITIES = ("LOW", "NORMAL", "HIGH", "URGENT")
DEPARTMENTS = tuple(
    d.strip() for d in (os.environ.get("DEPARTMENTS") or "DPS,SSG,SSI,FSI,CYBER").split(",") if d.strip()
)


def _bool(name, default=False):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


# Vercel sets VERCEL=1 in its environment.
ON_VERCEL = bool(os.environ.get("VERCEL"))


def _database_url():
    url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
    if not url and ON_VERCEL:
        # Vercel's disk is read-only except /tmp, which is wiped often: usable only until Postgres is connected.
        logging.getLogger(__name__).warning("DATABASE_URL is not set; using a temporary SQLite database in /tmp.")
        return "sqlite:////tmp/tracker.db"
    url = url or "sqlite:///instance/tracker.db"
    # Hosted Postgres (Vercel/Neon) gives postgres:// or postgresql:// URLs; use the psycopg 3 driver.
    for scheme in ("postgres://", "postgresql://"):
        if url.startswith(scheme):
            return "postgresql+psycopg://" + url[len(scheme):]
    # Resolve relative SQLite paths against the project folder, not the current directory.
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix):]
        if path and path != ":memory:" and not os.path.isabs(path):
            url = prefix + os.path.join(BASE_DIR, path).replace("\\", "/")
    return url


def _secret_key():
    key = os.environ.get("SECRET_KEY")
    if not key and ON_VERCEL:
        # Each serverless instance would get its own random key: logins and saved SMTP passwords would break.
        raise RuntimeError("SECRET_KEY must be set in the Vercel project's environment variables.")
    if not key:
        logging.getLogger(__name__).warning(
            "SECRET_KEY is not set; using a random key (sessions reset on restart)."
        )
        key = secrets.token_hex(32)
    return key


class Config:
    APP_NAME = os.environ.get("APP_NAME") or "FAXORA"
    SECRET_KEY = _secret_key()
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Hosted databases drop idle connections; check each one before use.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    APP_BASE_URL = (os.environ.get("APP_BASE_URL") or "http://localhost:5000").rstrip("/")

    SMTP_HOST = os.environ.get("SMTP_HOST", "")
    SMTP_PORT = int(os.environ.get("SMTP_PORT") or 25)
    SMTP_USER = os.environ.get("SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM = os.environ.get("SMTP_FROM") or "facilitytrack-noreply@example.com"
    SMTP_USE_TLS = _bool("SMTP_USE_TLS")
    SMTP_TIMEOUT = 10

    CATEGORY_MAILBOXES = {c: os.environ.get(f"CATEGORY_MAILBOX_{c}", "") for c in CATEGORIES}

    # Extra addresses alerted on every SOS (all agents are always alerted too).
    SOS_ALERT_EMAILS = [
        e.strip() for e in os.environ.get("SOS_ALERT_EMAILS", "").split(",") if e.strip()
    ]
    EMERGENCY_NUMBERS = os.environ.get("EMERGENCY_NUMBERS") or "Emergency 112 · Ambulance 108"

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", default=ON_VERCEL)
