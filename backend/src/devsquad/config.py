"""Paths and the loopback token.

Read from the environment on each call so tests can change them without
restarting the process. A ``.env`` file is not loaded automatically.
"""

from __future__ import annotations

import os
from pathlib import Path


def db_path() -> Path:
    """SQLite file for tasks and traces. Override with ``DEVSQUAD_DB``."""
    raw = os.environ.get("DEVSQUAD_DB")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".devsquad" / "devsquad.db"


def workspaces_dir() -> Path:
    """Parent directory for task worktrees. Override with ``DEVSQUAD_WORKSPACES``.

    The default lives under the user's home directory, outside any project
    repo. ``create_task`` still refuses a directory that resolves inside the
    target repo.
    """
    raw = os.environ.get("DEVSQUAD_WORKSPACES")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".devsquad" / "workspaces"


def server_token() -> str:
    """Shared secret for ``GET /health``. Empty means the server fails closed."""
    return os.environ.get("DEVSQUAD_TOKEN", "")
