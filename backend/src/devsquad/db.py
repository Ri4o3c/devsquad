"""SQLite schema for the control plane.

Two tables live in one local file:

* ``tasks`` — one row per isolated worktree
* ``traces`` — append-only events a future agent loop will write

Nothing in this module talks to a model or the network.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Required task columns stay in this order: id, repo_path, branch,
# worktree_path, status, created_at. ``title`` and ``base_commit`` are extra
# metadata the CLI needs; they are not a second source of truth for Git.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    repo_path TEXT NOT NULL,
    branch TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    base_commit TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    UNIQUE (task_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_traces_task_seq ON traces(task_id, seq);
"""


def init_db(db_path: Path) -> None:
    """Create the database file and tables if they are missing."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection. The caller closes it.

    ``isolation_level = None`` turns on autocommit so callers that need a
    multi-statement transaction can issue ``BEGIN`` themselves. Foreign keys
    are off by default in SQLite and the pragma is per connection, so it is
    set on every open.
    """
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn
