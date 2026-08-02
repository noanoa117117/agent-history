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
