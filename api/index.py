"""Vercel entry point: exposes the Flask app as a WSGI callable named `app`.

If the app cannot start (for example SECRET_KEY or DATABASE_URL missing), a plain error page
explains why instead of Vercel's generic 500, with any credentials in URLs masked.
"""
import logging
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app import create_app

    app = create_app()
except Exception as exc:  # noqa: BLE001 - report any start-up failure
    logging.getLogger(__name__).error("FAXORA failed to start:\n%s", traceback.format_exc())
    _reason = re.sub(r"://[^/\s@]+@", "://***@", f"{type(exc).__name__}: {exc}")[:500]

    def app(environ, start_response):
        body = (
            "FAXORA could not start on this deployment.\n\n"
            f"Reason: {_reason}\n\n"
            "Check the project's Environment Variables in Vercel (SECRET_KEY, DATABASE_URL), "
            "then redeploy.\n"
        ).encode()
        start_response("503 Service Unavailable", [("Content-Type", "text/plain; charset=utf-8"),
                                                   ("Content-Length", str(len(body)))])
        return [body]
