"""Event insertion and FTS-backed event maintenance."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from ..db import PathLike, connect_db, transaction
from ..sanitizer import SanitizedText, normalize_json, sanitize_text
from ..timeutil import parse_timestamp, utc_now_iso
from . import CommandError


def _read_content(content: Optional[str], content_file: Optional[str]) -> Optional[str]:
    if content is not None and content_file is not None:
        raise CommandError("--content and --content-file are mutually exclusive")
    if content_file is not None:
        try:
            return Path(content_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"could not read content file: {exc}") from exc
    return content


def add_event(
    db_path: Optional[PathLike],
    *,
    session_id: str,
    event_type: str,
    actor: str,
    content: Optional[str] = None,
    content_file: Optional[str] = None,
    content_json: Optional[str] = None,
    cwd: Optional[str] = None,
    exit_code: Optional[int] = None,
    occurred_at: Optional[str] = None,
    no_sanitize: bool = False,
) -> int:
    raw_content = _read_content(content, content_file)
    if raw_content is None and content_json is None:
        raise CommandError("one of --content, --content-file, or --content-json is required")

    content_result: Optional[SanitizedText] = None
    if raw_content is not None:
        content_result = (
            SanitizedText(raw_content, False) if no_sanitize else sanitize_text(raw_content)
        )

    json_result: Optional[SanitizedText] = None
    if content_json is not None:
        json_result = normalize_json(
            content_json, label="content_json", sanitize=not no_sanitize
        )

    if no_sanitize:
        sensitivity = "raw"
    elif (content_result and content_result.changed) or (json_result and json_result.changed):
        sensitivity = "sanitized"
    else:
        sensitivity = "clean"

    timestamp = parse_timestamp(occurred_at, label="occurred_at") if occurred_at else utc_now_iso()
    created_at = utc_now_iso()
    with connect_db(db_path) as connection:
        try:
            with transaction(connection, immediate=True):
                if connection.execute(
                    "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                ).fetchone() is None:
                    raise CommandError(f"session not found: {session_id}")
                sequence_row = connection.execute(
                    "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence "
                    "FROM events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                sequence_no = int(sequence_row["next_sequence"])
                cursor = connection.execute(
                    """
                    INSERT INTO events (
                        session_id, sequence_no, event_type, actor, content,
                        content_json, cwd, exit_code, occurred_at, sensitivity, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        sequence_no,
                        event_type,
                        actor,
                        content_result.text if content_result else None,
                        json_result.text if json_result else None,
                        cwd,
                        exit_code,
                        timestamp,
                        sensitivity,
                        created_at,
                    ),
                )
                return int(cursor.lastrowid)
        except CommandError:
            raise
        except sqlite3.IntegrityError as exc:
            raise CommandError(f"could not add event: {exc}") from exc


def delete_event(db_path: Optional[PathLike], event_id: int) -> None:
    """Delete an event; the schema trigger removes its FTS row."""

    with connect_db(db_path) as connection:
        with transaction(connection, immediate=True):
            cursor = connection.execute("DELETE FROM events WHERE id = ?", (event_id,))
            if cursor.rowcount == 0:
                raise CommandError(f"event not found: {event_id}")


def update_event_content(
    db_path: Optional[PathLike], event_id: int, content: str, *, no_sanitize: bool = False
) -> None:
    """Update event content; the schema trigger refreshes its FTS row."""

    result = SanitizedText(content, False) if no_sanitize else sanitize_text(content)
    sensitivity = "raw" if no_sanitize else ("sanitized" if result.changed else "clean")
    with connect_db(db_path) as connection:
        with transaction(connection, immediate=True):
            cursor = connection.execute(
                "UPDATE events SET content = ?, sensitivity = ? WHERE id = ?",
                (result.text, sensitivity, event_id),
            )
            if cursor.rowcount == 0:
                raise CommandError(f"event not found: {event_id}")
