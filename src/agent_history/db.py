"""SQLite connection, schema, and transaction helpers."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union


PathLike = Union[str, os.PathLike[str]]
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPOSITORY_ROOT / "data" / "agent_history.db"


def get_db_path(db_path: Optional[PathLike] = None) -> Path:
    """Resolve an explicit path, the environment override, or the default DB."""

    if db_path is not None:
        return Path(db_path).expanduser()
    configured = os.environ.get("AGENT_HISTORY_DB")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_DB_PATH


@contextmanager
def connect_db(
    db_path: Optional[PathLike] = None, *, timeout: float = 5.0
) -> Iterator[sqlite3.Connection]:
    """Open a configured SQLite connection and always close it."""

    path = get_db_path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=timeout, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        yield connection
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """Run a block in an explicit transaction."""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def schema_path() -> Path:
    return Path(__file__).with_name("schema.sql")


def fts5_available(connection: sqlite3.Connection) -> bool:
    """Check FTS5 by creating and removing a temporary virtual table."""

    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.agent_history_fts5_check USING fts5(content)"
        )
        connection.execute("DROP TABLE temp.agent_history_fts5_check")
    except sqlite3.OperationalError:
        return False
    return True


def apply_schema(connection: sqlite3.Connection) -> None:
    """Apply the idempotent schema to an open connection."""

    if not fts5_available(connection):
        raise RuntimeError(
            "SQLite FTS5 is unavailable. Install/use a Python build with ENABLE_FTS5."
        )
    connection.executescript(schema_path().read_text(encoding="utf-8"))
    migrate_schema(connection)
    # Rebuild the derived index on every safe re-initialization. This also
    # makes initialization repair an index left incomplete by an interrupted
    # older process without touching the primary events table. The rebuild runs
    # in one transaction so an interrupted init cannot leave the index empty.
    with transaction(connection, immediate=True):
        connection.execute("DELETE FROM events_fts")
        connection.execute(
            """
            INSERT INTO events_fts(event_id, session_id, content)
            SELECT id, session_id, coalesce(content, '') FROM events
            """
        )


def migrate_schema(connection: sqlite3.Connection) -> None:
    """Apply additive migrations without recreating or deleting user data."""

    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(events)").fetchall()
    }
    additions = {
        "source_event_id": "ALTER TABLE events ADD COLUMN source_event_id TEXT",
        "payload_size": "ALTER TABLE events ADD COLUMN payload_size INTEGER",
        "truncated": "ALTER TABLE events ADD COLUMN truncated INTEGER NOT NULL DEFAULT 0",
        "dedup_key": "ALTER TABLE events ADD COLUMN dedup_key TEXT",
    }
    with transaction(connection, immediate=True):
        for column, statement in additions.items():
            if column not in columns:
                connection.execute(statement)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_session_dedup_key "
            "ON events(session_id, dedup_key)"
        )


def init_database(db_path: Optional[PathLike] = None) -> Path:
    """Create the DB directory and apply the schema safely."""

    path = get_db_path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    with connect_db(path) as connection:
        apply_schema(connection)
        if not fts5_available(connection):
            raise RuntimeError("SQLite FTS5 is unavailable after schema initialization")
    if str(path) != ":memory:" and path.exists() and not path.is_symlink():
        # History can contain prompts, commands, and sanitized-but-sensitive
        # operational context. Keep the database private to the local user.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path
