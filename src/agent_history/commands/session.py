"""Session registration and display commands."""

from __future__ import annotations

import json
import socket
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from ..db import PathLike, connect_db, transaction
from ..sanitizer import normalize_json
from ..timeutil import utc_now_iso
from . import CommandError, format_table


def _metadata_value(metadata_json: Optional[str]) -> Optional[str]:
    if metadata_json is None:
        return None
    return normalize_json(metadata_json, label="metadata_json").text


def create_session(
    db_path: Optional[PathLike],
    *,
    source: str,
    source_session_id: Optional[str] = None,
    model: Optional[str] = None,
    initial_cwd: Optional[str] = None,
    host_name: Optional[str] = None,
    title: Optional[str] = None,
    metadata_json: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    identifier = session_id or str(uuid.uuid4())
    now = utc_now_iso()
    metadata = _metadata_value(metadata_json)
    with connect_db(db_path) as connection:
        try:
            with transaction(connection, immediate=True):
                connection.execute(
                    """
                    INSERT INTO sessions (
                        id, source, source_session_id, model, started_at, initial_cwd,
                        host_name, title, capture_status, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'capturing', ?, ?)
                    """,
                    (
                        identifier,
                        source,
                        source_session_id,
                        model,
                        now,
                        initial_cwd,
                        host_name or socket.gethostname(),
                        title,
                        metadata,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise CommandError(f"could not create session: {exc}") from exc
    return identifier


def end_session(db_path: Optional[PathLike], session_id: str) -> bool:
    """End a session. Return False when it was already ended."""

    with connect_db(db_path) as connection:
        with transaction(connection, immediate=True):
            row = connection.execute(
                "SELECT ended_at FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                raise CommandError(f"session not found: {session_id}")
            if row["ended_at"] is not None:
                return False
            connection.execute(
                """
                UPDATE sessions
                SET ended_at = ?, capture_status = 'completed'
                WHERE id = ?
                """,
                (utc_now_iso(), session_id),
            )
    return True


def list_sessions(
    db_path: Optional[PathLike], *, source: Optional[str] = None, limit: int = 20
) -> List[Dict[str, Any]]:
    if limit < 1:
        raise CommandError("limit must be at least 1")
    query = """
        SELECT id, source, title, started_at, ended_at, capture_status, initial_cwd
        FROM sessions
    """
    parameters: List[Any] = []
    if source:
        query += " WHERE source = ?"
        parameters.append(source)
    query += " ORDER BY started_at DESC LIMIT ?"
    parameters.append(limit)
    with connect_db(db_path) as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def show_session(db_path: Optional[PathLike], session_id: str) -> Dict[str, Any]:
    with connect_db(db_path) as connection:
        session_row = connection.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session_row is None:
            raise CommandError(f"session not found: {session_id}")
        target_rows = connection.execute(
            """
            SELECT t.id, t.target_type, t.slug, t.name, t.locator,
                   st.relation_type, st.confidence, st.assigned_by
            FROM session_targets AS st
            JOIN targets AS t ON t.id = st.target_id
            WHERE st.session_id = ?
            ORDER BY t.target_type, t.slug
            """,
            (session_id,),
        ).fetchall()
        event_rows = connection.execute(
            """
            SELECT id, sequence_no, event_type, actor, content,
                   cwd, exit_code, occurred_at, sensitivity
            FROM events
            WHERE session_id = ?
            ORDER BY sequence_no
            """,
            (session_id,),
        ).fetchall()
    return {
        "session": dict(session_row),
        "targets": [dict(row) for row in target_rows],
        "events": [dict(row) for row in event_rows],
    }


def render_session_list(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No sessions found."
    return format_table(
        rows,
        ["id", "source", "title", "started_at", "ended_at", "capture_status", "initial_cwd"],
    )


def render_session_show(data: Dict[str, Any]) -> str:
    session = data["session"]
    lines = [
        f"Session: {session['id']}",
        f"Source: {session['source']}",
        f"Source session ID: {session.get('source_session_id') or '-'}",
        f"Model: {session.get('model') or '-'}",
        f"Title: {session.get('title') or '-'}",
        f"Started: {session['started_at']}",
        f"Ended: {session.get('ended_at') or '-'}",
        f"Status: {session['capture_status']}",
        f"Working directory: {session.get('initial_cwd') or '-'}",
        f"Host: {session.get('host_name') or '-'}",
        "",
        f"Targets ({len(data['targets'])}):",
    ]
    if data["targets"]:
        lines.extend(
            "- {target_type}/{slug} ({relation_type}, assigned_by={assigned_by})".format(**target)
            for target in data["targets"]
        )
    else:
        lines.append("- None")
    lines.extend(["", f"Events ({len(data['events'])}):"])
    if not data["events"]:
        lines.append("- None")
    for event in data["events"]:
        lines.extend(
            [
                f"[{event['sequence_no']}] id={event['id']} {event['occurred_at']} "
                f"{event['event_type']}/{event['actor']} sensitivity={event['sensitivity']}",
                event["content"] if event.get("content") is not None else "(structured JSON only)",
            ]
        )
    return "\n".join(lines)
