"""Target registration and session-target relationship commands."""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from ..db import PathLike, connect_db, transaction
from ..sanitizer import normalize_json
from ..timeutil import utc_now_iso
from . import CommandError, format_table


def add_target(
    db_path: Optional[PathLike],
    *,
    target_type: str,
    slug: str,
    name: str,
    locator: Optional[str] = None,
    metadata_json: Optional[str] = None,
    update: bool = False,
) -> int:
    metadata = (
        normalize_json(metadata_json, label="metadata_json").text
        if metadata_json is not None
        else None
    )
    with connect_db(db_path) as connection:
        with transaction(connection, immediate=True):
            existing = connection.execute(
                "SELECT id FROM targets WHERE target_type = ? AND slug = ?",
                (target_type, slug),
            ).fetchone()
            if existing is not None:
                target_id = int(existing["id"])
                if update:
                    connection.execute(
                        """
                        UPDATE targets
                        SET name = ?, locator = ?, metadata_json = ?
                        WHERE id = ?
                        """,
                        (name, locator, metadata, target_id),
                    )
                return target_id
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO targets
                        (target_type, slug, name, locator, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (target_type, slug, name, locator, metadata, utc_now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise CommandError(f"could not add target: {exc}") from exc
            return int(cursor.lastrowid)


def list_targets(
    db_path: Optional[PathLike], *, target_type: Optional[str] = None
) -> List[Dict[str, Any]]:
    query = """
        SELECT id, target_type, slug, name, locator, created_at
        FROM targets
    """
    parameters: List[Any] = []
    if target_type:
        query += " WHERE target_type = ?"
        parameters.append(target_type)
    query += " ORDER BY target_type, slug"
    with connect_db(db_path) as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def render_target_list(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No targets found."
    return format_table(rows, ["id", "target_type", "slug", "name", "locator", "created_at"])


def add_session_target(
    db_path: Optional[PathLike],
    *,
    session_id: str,
    target_id: int,
    relation_type: str = "related",
    confidence: Optional[float] = None,
    assigned_by: str = "manual",
) -> None:
    if confidence is not None and not 0.0 <= confidence <= 1.0:
        raise CommandError("confidence must be between 0.0 and 1.0")
    with connect_db(db_path) as connection:
        with transaction(connection, immediate=True):
            if connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone() is None:
                raise CommandError(f"session not found: {session_id}")
            if connection.execute("SELECT 1 FROM targets WHERE id = ?", (target_id,)).fetchone() is None:
                raise CommandError(f"target not found: {target_id}")
            connection.execute(
                """
                INSERT INTO session_targets
                    (session_id, target_id, relation_type, confidence, assigned_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, target_id) DO UPDATE SET
                    relation_type = excluded.relation_type,
                    confidence = excluded.confidence,
                    assigned_by = excluded.assigned_by,
                    created_at = excluded.created_at
                """,
                (session_id, target_id, relation_type, confidence, assigned_by, utc_now_iso()),
            )
