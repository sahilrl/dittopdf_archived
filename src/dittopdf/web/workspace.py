"""Per-session temporary workspaces.

Each browser session gets one directory named by a random token under the
configured root. Uploaded files are stored under fixed names inside it
(``original.pdf``, ``second.pdf``, ``output.pdf``), so no user-provided
filename or metadata value is ever used to build a filesystem path. Expired
workspaces are deleted automatically.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
FILES = {"original": "original.pdf", "second": "second.pdf", "output": "output.pdf"}


class Workspace:
    def __init__(self, root: Path, token: str) -> None:
        if not TOKEN_RE.match(token):
            raise ValueError("invalid workspace token")
        self.token = token
        self.dir = root / token

    # -- lifecycle --------------------------------------------------------------------

    @classmethod
    def create(cls, root: Path) -> "Workspace":
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        ws = cls(root, secrets.token_urlsafe(24))
        ws.dir.mkdir(mode=0o700)
        return ws

    def exists(self) -> bool:
        return self.dir.is_dir()

    def touch(self) -> None:
        if self.exists():
            os.utime(self.dir)

    def destroy(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- files ------------------------------------------------------------------------

    def path(self, role: str) -> Path:
        return self.dir / FILES[role]

    def save_upload(self, role: str, stream: Any) -> Path:
        """Write an upload to a fresh temporary name, then move it into place."""
        dest = self.path(role)
        tmp = self.dir / f".{role}.{secrets.token_hex(8)}.part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            shutil.copyfileobj(stream, f, 1024 * 1024)
        os.replace(tmp, dest)
        return dest

    def drop(self, *roles: str) -> None:
        for role in roles:
            self.path(role).unlink(missing_ok=True)

    # -- JSON documents -----------------------------------------------------------------

    def _json_path(self, name: str) -> Path:
        if not re.fullmatch(r"[a-z_]+", name):
            raise ValueError(name)
        return self.dir / f"{name}.json"

    def write(self, name: str, data: Any) -> None:
        p = self._json_path(name)
        tmp = p.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)

    def read(self, name: str, default: Any = None) -> Any:
        p = self._json_path(name)
        if not p.exists():
            return default
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    def delete(self, *names: str) -> None:
        for n in names:
            self._json_path(n).unlink(missing_ok=True)


_last_sweep = [0.0]


def sweep(root: Path, ttl: int, *, every: int = 60) -> int:
    """Delete workspaces untouched for ``ttl`` seconds (at most once per ``every`` seconds)."""
    now = time.time()
    if now - _last_sweep[0] < every or not root.is_dir():
        return 0
    _last_sweep[0] = now
    removed = 0
    for d in root.iterdir():
        if not (d.is_dir() and TOKEN_RE.match(d.name)):
            continue
        try:
            if now - d.stat().st_mtime > ttl:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except FileNotFoundError:
            continue
    return removed
