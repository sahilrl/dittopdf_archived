"""Configuration, overridable through ``DITTOPDF_*`` environment variables."""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Config:
    # Signs the session cookie, which holds only a random workspace id and a CSRF token.
    # Set DITTOPDF_SECRET_KEY when running more than one worker process.
    SECRET_KEY = os.environ.get("DITTOPDF_SECRET_KEY") or secrets.token_hex(32)
    # Per-upload limit; Flask rejects larger requests with 413 before they reach the app.
    MAX_UPLOAD_MB = _int("DITTOPDF_MAX_UPLOAD_MB", 100)
    MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024
    # The edit form only posts changed fields, but allow large forms anyway.
    MAX_FORM_PARTS = _int("DITTOPDF_MAX_FORM_PARTS", 20_000)
    MAX_FORM_MEMORY_SIZE = 50 * 1024 * 1024
    # Isolated per-session directories live under this root and expire after the TTL.
    WORK_DIR = Path(os.environ.get("DITTOPDF_WORK_DIR") or Path(tempfile.gettempdir()) / "dittopdf-work")
    WORKSPACE_TTL_SECONDS = _int("DITTOPDF_WORKSPACE_TTL", 3600)
    # Pages (and their annotations) inspected individually; larger documents are summarized.
    MAX_DETAIL_PAGES = _int("DITTOPDF_MAX_DETAIL_PAGES", 200)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
