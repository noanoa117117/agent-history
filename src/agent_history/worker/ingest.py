"""Batch ingestion of spool files into SQLite.

All of the expensive work the hook skips happens here, off Claude Code's
critical path: JSON parsing, sanitizing, truncation, and the fsync-bearing
commit. Batching is the point -- one transaction per batch means one fsync for
up to `DEFAULT_BATCH_SIZE` events instead of one fsync each, which on a
rotational disk is the difference between ~170ms and ~3ms per event.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..capture import spool
from ..capture.claude_hook import EVENT_MAPPING, HookConfig, PreparedEvent, prepare_event
from ..timeutil import utc_now_iso


DEFAULT_BATCH_SIZE = 50


class PermanentError(Exception):
    """An event that will never succeed: quarantine it, do not retry."""


@dataclass(frozen=True)
class SpoolItem:
    path: str
    uid: str
    event_name: str
    recorded_at_ns: int
    input_size: int
    capture_source: str
    payload: Dict[str, Any]


@dataclass
class BatchResult:
    inserted: int = 0
    duplicates: int = 0
    quarantined: int = 0

    @property
    def handled(self) -> int:
        return self.inserted + self.duplicates + self.quarantined


def _iso_from_ns(recorded_at_ns: int) -> str:
    seconds, remainder = divmod(int(recorded_at_ns), 1_000_000_000)
    moment = datetime.fromtimestamp(seconds, timezone.utc)
    return moment.replace(microsecond=remainder // 1000).isoformat()


def parse_spool_file(path: str) -> SpoolItem:
    """Read and validate one spool file, or raise PermanentError."""

    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise PermanentError(f"unreadable spool file: {type(exc).__name__}") from exc

    header_bytes, payload_bytes = spool.split_file(raw)
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except Exception as exc:
        raise PermanentError(f"invalid spool header: {type(exc).__name__}") from exc
    if not isinstance(header, dict):
        raise PermanentError("spool header must be a JSON object")
    if header.get("v") != spool.SCHEMA_VERSION:
        raise PermanentError(f"unsupported spool schema_version: {header.get('v')!r}")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        raise PermanentError(f"invalid hook JSON: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise PermanentError("hook payload must be a JSON object")

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise PermanentError("hook payload is missing session_id")

    # The hook takes the event name from argv because it cannot afford to parse
    # JSON; cross-check it against the payload now that we can.
    header_event = header.get("ev")
    payload_event = payload.get("hook_event_name")
    event_name = payload_event or header_event
    if header_event and payload_event and header_event != payload_event:
        raise PermanentError(
            f"event name mismatch: header={header_event!r} payload={payload_event!r}"
        )
    if event_name not in EVENT_MAPPING:
        raise PermanentError(f"unsupported hook event: {event_name!r}")

    capture_source = str(header.get("src") or "claude-code")
    if capture_source not in {"claude-code", "codex"}:
        raise PermanentError(f"unsupported capture source: {capture_source!r}")

    return SpoolItem(
        path=path,
        uid=str(header.get("uid") or os.path.basename(path)),
        event_name=str(event_name),
        recorded_at_ns=int(header.get("ts_ns") or 0),
        input_size=int(header.get("size") or len(payload_bytes)),
        capture_source=capture_source,
        payload=payload,
    )


def quarantine(root: str, path: str, reason: str) -> Optional[str]:
    """Move a permanently broken event aside so it cannot stall the batch."""

    failed_dir = spool.subdir(root, spool.DIR_FAILED)
    try:
        spool.ensure_dirs(root)
        destination = os.path.join(failed_dir, os.path.basename(path))
        os.replace(path, destination)
        with open(destination + ".reason", "w", encoding="utf-8") as handle:
            handle.write(f"{utc_now_iso()} {reason[:500]}\n")
        os.chmod(destination + ".reason", spool.FILE_MODE)
        return destination
    except OSError:
        # Losing a broken event is acceptable; blocking the worker is not.
        try:
            os.unlink(path)
        except OSError:
            pass
        return None


def _resolve_session(
    connection: sqlite3.Connection,
    capture_source: str,
    source_session_id: str,
    payload: Mapping[str, Any],
    occurred_at: str,
) -> str:
    row = connection.execute(
        """
        SELECT id FROM sessions
        WHERE source = ? AND source_session_id = ?
        ORDER BY created_at LIMIT 1
        """,
        (capture_source, source_session_id),
    ).fetchone()
    if row is not None:
        session_id = str(row["id"])
        connection.execute(
            """
            UPDATE sessions
            SET model = COALESCE(model, ?),
                initial_cwd = COALESCE(initial_cwd, ?),
                title = COALESCE(title, ?)
            WHERE id = ?
            """,
            (
                payload.get("model"),
                payload.get("cwd"),
                payload.get("session_title"),
                session_id,
            ),
        )
        return session_id

    session_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO sessions (
            id, source, source_session_id, model, started_at,
            initial_cwd, host_name, title, capture_status, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'capturing', ?, ?)
        """,
        (
            session_id,
            capture_source,
            source_session_id,
            payload.get("model"),
            occurred_at,
            payload.get("cwd"),
            os.uname().nodename if hasattr(os, "uname") else None,
            payload.get("session_title"),
            json.dumps(
                {"agent_type": payload["agent_type"]} if payload.get("agent_type") else {},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            utc_now_iso(),
        ),
    )
    return session_id


def _link_session_to_cwd_target(
    connection: sqlite3.Connection, session_id: str, cwd: Any, occurred_at: str
) -> None:
    """Link a session to the most specific registered repository containing cwd."""

    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return
    normalized_cwd = os.path.realpath(cwd)
    match = None
    for row in connection.execute(
        "SELECT id, locator FROM targets "
        "WHERE target_type = 'repository' AND locator IS NOT NULL"
    ).fetchall():
        root = os.path.realpath(str(row["locator"]))
        try:
            contained = os.path.commonpath((normalized_cwd, root)) == root
        except ValueError:
            contained = False
        if contained and (match is None or len(root) > match[0]):
            match = (len(root), int(row["id"]))
    if match is None:
        return
    connection.execute(
        """
        INSERT INTO session_targets
            (session_id, target_id, relation_type, confidence, assigned_by, created_at)
        VALUES (?, ?, 'worked_on', 1.0, 'cwd-auto', ?)
        ON CONFLICT(session_id, target_id) DO NOTHING
        """,
        (session_id, match[1], occurred_at),
    )


def ingest_batch(
    connection: sqlite3.Connection,
    items: Sequence[Tuple[SpoolItem, PreparedEvent]],
) -> BatchResult:
    """Insert a whole batch inside the caller's open transaction."""

    result = BatchResult()
    sequences: Dict[str, int] = {}
    sessions: Dict[str, str] = {}

    for item, prepared in items:
        source_session_id = str(item.payload["session_id"])
        occurred_at = _iso_from_ns(item.recorded_at_ns) if item.recorded_at_ns else utc_now_iso()

        session_key = f"{item.capture_source}:{source_session_id}"
        session_id = sessions.get(session_key)
        if session_id is None:
            session_id = _resolve_session(
                connection, item.capture_source, source_session_id, item.payload, occurred_at
            )
            sessions[session_key] = session_id

        _link_session_to_cwd_target(
            connection, session_id, item.payload.get("cwd"), occurred_at
        )

        if session_id not in sequences:
            # One MAX() per session per batch instead of one per event.
            sequences[session_id] = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence_no), 0) FROM events WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )

        event_type, actor = EVENT_MAPPING[prepared.event_name]
        if item.capture_source == "codex" and actor == "claude":
            actor = "codex"
        next_sequence = sequences[session_id] + 1
        cursor = connection.execute(
            """
            INSERT INTO events (
                session_id, sequence_no, event_type, actor, content,
                content_json, source_event_id, payload_size, truncated,
                dedup_key, cwd, exit_code, occurred_at, sensitivity, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, dedup_key) DO NOTHING
            """,
            (
                session_id,
                next_sequence,
                event_type,
                actor,
                prepared.content,
                prepared.content_json,
                prepared.source_event_id,
                len(prepared.content_json.encode("utf-8")),
                int(prepared.truncated),
                prepared.dedup_key,
                item.payload.get("cwd"),
                item.payload.get("exit_code"),
                occurred_at,
                prepared.sensitivity,
                utc_now_iso(),
            ),
        )
        if cursor.rowcount:
            # Only advance on a real insert so a duplicate cannot punch a hole
            # in the per-session sequence.
            sequences[session_id] = next_sequence
            result.inserted += 1
        else:
            result.duplicates += 1

        if prepared.event_name == "SessionEnd":
            connection.execute(
                "UPDATE sessions SET ended_at = ?, capture_status = 'completed' WHERE id = ?",
                (occurred_at, session_id),
            )

    return result


def claim_pending(root: str, limit: int = DEFAULT_BATCH_SIZE) -> List[str]:
    """Take the oldest pending files. Lexical order is chronological order."""

    pending = spool.subdir(root, spool.DIR_PENDING)
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(pending)
            if entry.is_file() and entry.name.endswith(spool.FILE_SUFFIX)
        )
    except FileNotFoundError:
        return []
    return [os.path.join(pending, name) for name in names[:limit]]


def prepare_items(
    root: str, paths: Sequence[str], config: HookConfig
) -> Tuple[List[Tuple[SpoolItem, PreparedEvent]], int]:
    """Parse and sanitize spool files, quarantining the broken ones."""

    prepared: List[Tuple[SpoolItem, PreparedEvent]] = []
    quarantined = 0
    for path in paths:
        try:
            item = parse_spool_file(path)
            payload = dict(item.payload)
            payload["_agent_history_source"] = item.capture_source
            event = prepare_event(item.event_name, payload, config, item.input_size)
        except PermanentError as exc:
            quarantine(root, path, str(exc))
            quarantined += 1
            continue
        except Exception as exc:  # noqa: BLE001 - a bad payload must not stall the worker
            quarantine(root, path, f"{type(exc).__name__}: {exc}")
            quarantined += 1
            continue
        prepared.append((item, event))
    return prepared, quarantined
