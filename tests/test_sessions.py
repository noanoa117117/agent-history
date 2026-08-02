import tempfile
import unittest
from pathlib import Path

from agent_history.commands import CommandError
from agent_history.commands.session import create_session, end_session, show_session
from agent_history.db import init_database, connect_db


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        init_database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_and_end_session(self):
        session_id = create_session(
            self.db_path,
            source="codex",
            model="gpt-test",
            initial_cwd="/tmp/work",
            host_name="test-host",
            title="A test session",
            metadata_json='{"kind":"test"}',
        )
        self.assertTrue(session_id)
        data = show_session(self.db_path, session_id)
        self.assertEqual(data["session"]["source"], "codex")
        self.assertEqual(data["session"]["model"], "gpt-test")
        self.assertEqual(data["session"]["initial_cwd"], "/tmp/work")
        self.assertEqual(data["session"]["title"], "A test session")
        self.assertEqual(end_session(self.db_path, session_id), True)
        self.assertEqual(end_session(self.db_path, session_id), False)
        with connect_db(self.db_path) as connection:
            row = connection.execute(
                "SELECT ended_at, capture_status FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            self.assertIsNotNone(row["ended_at"])
            self.assertEqual(row["capture_status"], "completed")

    def test_end_missing_session_fails(self):
        with self.assertRaises(CommandError):
            end_session(self.db_path, "does-not-exist")

    def test_invalid_metadata_json_fails(self):
        with self.assertRaises(ValueError):
            create_session(self.db_path, source="manual-import", metadata_json="not-json")
