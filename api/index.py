"""Vercel entry point (used if the project is not detected as zero-config Flask).

Exposes the same WSGI app as app.py's top-level `app`; start-up errors are shown as a plain page.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as faxora  # noqa: E402

app = getattr(faxora, "app", None) or faxora.safe_create_app()
