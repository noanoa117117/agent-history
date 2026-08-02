import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_history.db import connect_db, fts5_available, init_database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_initialization_is_idempotent_and_has_required_schema(self):
        init_database(self.db_path)
        init_database(self.db_path)
        with connect_db(self.db_path) as connection:
            names = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                )
            }
            self.assertTrue({"sessions", "events", "targets", "session_targets", "events_fts"}.issubset(names))
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertTrue(fts5_available(connection))

    def test_foreign_key_rejects_unknown_session(self):
        init_database(self.db_path)
        with connect_db(self.db_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO events(session_id, sequence_no, event_type, actor, occurred_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("missing", 1, "error", "system", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                )

    def test_existing_stage1_schema_is_migrated_without_data_loss(self):
        # Reproduce the pre-Stage-2 events table: initialization must add
        # columns instead of recreating or deleting the primary event data.
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_session_id TEXT,
                    model TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    initial_cwd TEXT,
                    host_name TEXT,
                    title TEXT,
                    capture_status TEXT NOT NULL DEFAULT 'capturing',
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    content TEXT,
                    content_json TEXT,
                    cwd TEXT,
                    exit_code INTEGER,
                    occurred_at TEXT NOT NULL,
                    sensitivity TEXT NOT NULL DEFAULT 'unchecked',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                    UNIQUE (session_id, sequence_no)
                );
                CREATE VIRTUAL TABLE events_fts USING fts5(
                    event_id UNINDEXED, session_id UNINDEXED, content
                );
                INSERT INTO sessions(
                    id, source, started_at, capture_status, created_at
                ) VALUES (
                    'legacy-session', 'manual-import',
                    '2026-01-01T00:00:00+00:00', 'capturing',
                    '2026-01-01T00:00:00+00:00'
                );
                INSERT INTO events(
                    session_id, sequence_no, event_type, actor, content,
                    occurred_at, created_at
                ) VALUES (
                    'legacy-session', 1, 'session_note', 'human',
                    'legacy migration fixture',
                    '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00'
                );
                """
            )
        init_database(self.db_path)
        with connect_db(self.db_path) as connection:
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(events)")
            }
            self.assertTrue(
                {"source_event_id", "payload_size", "truncated", "dedup_key"}.issubset(columns)
            )
            self.assertEqual(
                connection.execute("SELECT content FROM events WHERE id = 1").fetchone()[0],
                "legacy migration fixture",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH ?",
                    ("migration",),
                ).fetchone()[0],
                1,
            )
