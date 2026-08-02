import json
import tempfile
import unittest
from pathlib import Path

from agent_history.commands import CommandError
from agent_history.commands.event import add_event, delete_event, update_event_content
from agent_history.commands.search import search_events
from agent_history.commands.session import create_session
from agent_history.db import connect_db, init_database


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        init_database(self.db_path)
        self.session_one = create_session(self.db_path, source="codex")
        self.session_two = create_session(self.db_path, source="human-shell")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sequence_starts_at_one_per_session(self):
        first = add_event(
            self.db_path,
            session_id=self.session_one,
            event_type="user_prompt",
            actor="human",
            content="first cockpit note",
        )
        second = add_event(
            self.db_path,
            session_id=self.session_one,
            event_type="assistant_message",
            actor="codex",
            content="second cockpit note",
        )
        other = add_event(
            self.db_path,
            session_id=self.session_two,
            event_type="command",
            actor="human",
            content="other session",
        )
        with connect_db(self.db_path) as connection:
            rows = connection.execute(
                "SELECT id, session_id, sequence_no FROM events ORDER BY id"
            ).fetchall()
        self.assertEqual([row["sequence_no"] for row in rows], [1, 2, 1])
        self.assertEqual([row["id"] for row in rows], [first, second, other])

    def test_unknown_session_is_rejected(self):
        with self.assertRaises(CommandError):
            add_event(
                self.db_path,
                session_id="missing",
                event_type="error",
                actor="system",
                content="no session",
            )

    def test_content_json_is_validated_and_not_double_encoded(self):
        event_id = add_event(
            self.db_path,
            session_id=self.session_one,
            event_type="tool_result",
            actor="tool",
            content_json='{"ok":true,"items":[1,2]}',
        )
        with connect_db(self.db_path) as connection:
            row = connection.execute(
                "SELECT content, content_json FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        self.assertIsNone(row["content"])
        self.assertEqual(json.loads(row["content_json"]), {"ok": True, "items": [1, 2]})
        with self.assertRaises(ValueError):
            add_event(
                self.db_path,
                session_id=self.session_one,
                event_type="tool_result",
                actor="tool",
                content_json="{invalid",
            )

    def _fts_match(self, term):
        """Query the FTS index directly.

        search_events() also has a LIKE fallback for unsegmented text, so it
        cannot prove the FTS index is in sync. These assertions must fail if a
        sync trigger is missing.
        """

        with connect_db(self.db_path) as connection:
            rows = connection.execute(
                "SELECT event_id FROM events_fts WHERE events_fts MATCH ?",
                ('"' + term + '"',),
            ).fetchall()
        return [int(row["event_id"]) for row in rows]

    def _fts_row_count(self):
        with connect_db(self.db_path) as connection:
            return connection.execute("SELECT count(*) FROM events_fts").fetchone()[0]

    def test_fts_insert_update_and_delete_sync(self):
        event_id = add_event(
            self.db_path,
            session_id=self.session_one,
            event_type="session_note",
            actor="human",
            content="unique-alpha phrase",
        )
        self.assertEqual(self._fts_row_count(), 1)
        self.assertEqual(self._fts_match("unique-alpha"), [event_id])
        self.assertEqual(len(search_events(self.db_path, "unique-alpha")), 1)

        update_event_content(self.db_path, event_id, "unique-beta phrase")
        self.assertEqual(self._fts_row_count(), 1)
        self.assertEqual(self._fts_match("unique-alpha"), [])
        self.assertEqual(self._fts_match("unique-beta"), [event_id])
        self.assertEqual(search_events(self.db_path, "unique-alpha"), [])
        self.assertEqual(len(search_events(self.db_path, "unique-beta")), 1)

        delete_event(self.db_path, event_id)
        self.assertEqual(self._fts_row_count(), 0)
        self.assertEqual(self._fts_match("unique-beta"), [])
        self.assertEqual(search_events(self.db_path, "unique-beta"), [])

    def test_reinitialization_rebuilds_the_fts_index(self):
        event_id = add_event(
            self.db_path,
            session_id=self.session_one,
            event_type="session_note",
            actor="human",
            content="rebuildable phrase",
        )
        with connect_db(self.db_path) as connection:
            connection.execute("DELETE FROM events_fts")
        self.assertEqual(self._fts_match("rebuildable"), [])
        init_database(self.db_path)
        self.assertEqual(self._fts_match("rebuildable"), [event_id])

    def test_no_sanitize_marks_raw(self):
        event_id = add_event(
            self.db_path,
            session_id=self.session_one,
            event_type="session_note",
            actor="human",
            content="api_key: demo-value",
            no_sanitize=True,
        )
        with connect_db(self.db_path) as connection:
            self.assertEqual(
                connection.execute("SELECT sensitivity FROM events WHERE id = ?", (event_id,)).fetchone()[0],
                "raw",
            )
