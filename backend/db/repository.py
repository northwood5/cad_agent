# -*- coding: utf-8 -*-
"""
CRUD helpers over the SQLite schema (see :mod:`db.database`).

All functions are thin, synchronous wrappers around the shared connection.
Writes are serialised with the module lock; reads rely on WAL. Rows are
returned as plain dicts so they serialise straight to JSON.
"""
from __future__ import annotations

import json
from typing import Any

from .database import get_conn, lock


def _row(r) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def _rows(rs) -> list[dict[str, Any]]:
    return [dict(r) for r in rs]


# ── Users ─────────────────────────────────────────────────────────────────────

def get_or_create_user(username: str) -> dict[str, Any]:
    """Return the user with *username*, creating it if absent (no password)."""
    username = username.strip()
    if not username:
        raise ValueError("username must not be empty")
    conn = get_conn()
    with lock():
        conn.execute(
            "INSERT OR IGNORE INTO users(username) VALUES (?)", (username,)
        )
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return _row(row)


def get_user(user_id: int) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return _row(row)


# ── Projects ──────────────────────────────────────────────────────────────────

def create_project(user_id: int, name: str) -> dict[str, Any]:
    conn = get_conn()
    with lock():
        cur = conn.execute(
            "INSERT INTO projects(user_id, name) VALUES (?, ?)", (user_id, name)
        )
        pid = cur.lastrowid
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
    return _row(row)


def list_projects(user_id: int) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM projects WHERE user_id = ? ORDER BY updated_at DESC",
        (user_id,),
    ).fetchall()
    return _rows(rows)


def get_project(project_id: int) -> dict[str, Any] | None:
    row = get_conn().execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    return _row(row)


def rename_project(project_id: int, name: str) -> None:
    conn = get_conn()
    with lock():
        conn.execute(
            "UPDATE projects SET name = ?, updated_at = datetime('now') WHERE id = ?",
            (name, project_id),
        )


def touch_project(project_id: int) -> None:
    """Bump updated_at so recent projects sort first."""
    conn = get_conn()
    with lock():
        conn.execute(
            "UPDATE projects SET updated_at = datetime('now') WHERE id = ?",
            (project_id,),
        )


def delete_project(project_id: int) -> None:
    conn = get_conn()
    with lock():
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# ── Messages ──────────────────────────────────────────────────────────────────

def add_message(project_id: int, role: str, content: str) -> dict[str, Any]:
    conn = get_conn()
    with lock():
        cur = conn.execute(
            "INSERT INTO messages(project_id, role, content) VALUES (?, ?, ?)",
            (project_id, role, content),
        )
        mid = cur.lastrowid
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    return _row(row)


def list_messages(project_id: int) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM messages WHERE project_id = ? ORDER BY id ASC",
        (project_id,),
    ).fetchall()
    return _rows(rows)


# ── Workflow runs ─────────────────────────────────────────────────────────────

def create_run(project_id: int, user_request: str, status: str = "running") -> dict[str, Any]:
    conn = get_conn()
    with lock():
        cur = conn.execute(
            "INSERT INTO workflow_runs(project_id, user_request, status) VALUES (?, ?, ?)",
            (project_id, user_request, status),
        )
        rid = cur.lastrowid
        row = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (rid,)).fetchone()
    return _row(row)


def set_run_status(run_id: int, status: str) -> None:
    conn = get_conn()
    with lock():
        conn.execute(
            "UPDATE workflow_runs SET status = ? WHERE id = ?", (status, run_id)
        )


def list_runs(project_id: int) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM workflow_runs WHERE project_id = ? ORDER BY id DESC",
        (project_id,),
    ).fetchall()
    return _rows(rows)


def get_run_with_nodes(run_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    run = conn.execute("SELECT * FROM workflow_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return None
    nodes = conn.execute(
        "SELECT * FROM workflow_nodes WHERE run_id = ? ORDER BY sequence ASC", (run_id,)
    ).fetchall()
    out = _row(run)
    out["nodes"] = _rows(nodes)
    return out


# ── Workflow nodes ────────────────────────────────────────────────────────────

def create_node(
    run_id: int,
    node_key: str,
    agent: str,
    title: str,
    instruction: str,
    depends_on: list[str] | None = None,
    sequence: int = 0,
) -> dict[str, Any]:
    conn = get_conn()
    with lock():
        cur = conn.execute(
            """INSERT INTO workflow_nodes
               (run_id, node_key, agent, title, instruction, depends_on, sequence)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, node_key, agent, title, instruction,
             json.dumps(depends_on or []), sequence),
        )
        nid = cur.lastrowid
        row = conn.execute("SELECT * FROM workflow_nodes WHERE id = ?", (nid,)).fetchone()
    return _row(row)


def set_node_status(
    node_id: int,
    status: str,
    summary: str | None = None,
    *,
    mark_start: bool = False,
    mark_finish: bool = False,
) -> None:
    sets = ["status = ?"]
    params: list[Any] = [status]
    if summary is not None:
        sets.append("summary = ?")
        params.append(summary)
    if mark_start:
        sets.append("started_at = datetime('now')")
    if mark_finish:
        sets.append("finished_at = datetime('now')")
    params.append(node_id)
    conn = get_conn()
    with lock():
        conn.execute(
            f"UPDATE workflow_nodes SET {', '.join(sets)} WHERE id = ?", params
        )


# ── Artifacts ─────────────────────────────────────────────────────────────────

def add_artifact(
    project_id: int,
    kind: str,
    filename: str,
    path: str,
    run_id: int | None = None,
    node_id: int | None = None,
) -> dict[str, Any]:
    conn = get_conn()
    with lock():
        cur = conn.execute(
            """INSERT INTO artifacts(project_id, run_id, node_id, kind, filename, path)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (project_id, run_id, node_id, kind, filename, path),
        )
        aid = cur.lastrowid
        row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (aid,)).fetchone()
    return _row(row)


def list_artifacts(project_id: int) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM artifacts WHERE project_id = ? ORDER BY id DESC",
        (project_id,),
    ).fetchall()
    return _rows(rows)


# ── Scripts ───────────────────────────────────────────────────────────────────

def add_script(
    project_id: int,
    agent: str,
    software: str,
    language: str,
    content: str,
    filename: str | None = None,
    run_id: int | None = None,
    node_id: int | None = None,
) -> dict[str, Any]:
    conn = get_conn()
    with lock():
        cur = conn.execute(
            """INSERT INTO scripts
               (project_id, run_id, node_id, agent, software, language, filename, content)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, run_id, node_id, agent, software, language, filename, content),
        )
        sid = cur.lastrowid
        row = conn.execute("SELECT * FROM scripts WHERE id = ?", (sid,)).fetchone()
    return _row(row)


def list_scripts(project_id: int) -> list[dict[str, Any]]:
    rows = get_conn().execute(
        "SELECT * FROM scripts WHERE project_id = ? ORDER BY id DESC",
        (project_id,),
    ).fetchall()
    return _rows(rows)
