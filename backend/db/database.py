# -*- coding: utf-8 -*-
"""
SQLite connection + schema management.

Lightweight persistence for users, projects, chat messages, workflow runs,
their nodes, produced artifacts, and generated CAx scripts. Uses the stdlib
``sqlite3`` module (no extra dependency) with WAL mode for concurrent reads.

The database file lives at ``backend/cad_agent.db`` and is created/migrated
automatically on first use via :func:`init_db`.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # backend/
DB_PATH = BASE_DIR / "cad_agent.db"

# A single module-level connection guarded by a lock. SQLite handles
# multi-threaded access poorly with shared connections, so we serialise
# writes with a lock and enable WAL so reads stay fast.
_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


# ── Schema ──────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT UNIQUE NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,                 -- user | agent | system
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_request  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending|running|success|failed
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workflow_nodes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    node_key     TEXT NOT NULL,               -- stable id within the run (e.g. "n1")
    agent        TEXT NOT NULL,               -- cad | mesh | cae
    title        TEXT NOT NULL,
    instruction  TEXT NOT NULL,
    depends_on   TEXT NOT NULL DEFAULT '[]',  -- JSON array of node_keys
    sequence     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'pending',
    summary      TEXT,
    started_at   TEXT,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id      INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL,
    node_id     INTEGER REFERENCES workflow_nodes(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL,                -- stl | step | obj | mesh | result
    filename    TEXT NOT NULL,
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    run_id      INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL,
    node_id     INTEGER REFERENCES workflow_nodes(id) ON DELETE SET NULL,
    agent       TEXT NOT NULL,
    software    TEXT NOT NULL,                -- freecad | gmsh | calculix
    language    TEXT NOT NULL,                -- python | geo | inp
    filename    TEXT,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_projects_user      ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_project   ON messages(project_id);
CREATE INDEX IF NOT EXISTS idx_runs_project       ON workflow_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_nodes_run          ON workflow_nodes(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_project  ON artifacts(project_id);
CREATE INDEX IF NOT EXISTS idx_scripts_project    ON scripts(project_id);
"""


# ── Connection management ────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    """Return the shared SQLite connection, creating it on first call."""
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                _conn = sqlite3.connect(
                    str(DB_PATH),
                    check_same_thread=False,
                    isolation_level=None,          # autocommit; we manage txns
                )
                _conn.row_factory = sqlite3.Row
                _conn.execute("PRAGMA journal_mode=WAL")
                _conn.execute("PRAGMA foreign_keys=ON")
                _conn.execute("PRAGMA busy_timeout=5000")
    return _conn


def init_db() -> None:
    """Create all tables/indexes if they do not yet exist."""
    conn = get_conn()
    with _lock:
        conn.executescript(_SCHEMA)


# Re-export the lock so the repository can serialise multi-statement writes.
def lock() -> threading.RLock:
    return _lock
