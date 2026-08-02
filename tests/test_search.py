import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_history.cli import main
from agent_history.commands.event import add_event
from agent_history.commands.search import search_events
from agent_history.commands.session import create_session
from agent_history.db import init_database


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        init_database(self.db_path)
        self.codex = create_session(self.db_path, source="codex", title="Codex work")
        self.human = create_session(self.db_path, source="human-shell", title="Shell work")
        add_event(
            self.db_path,
            session_id=self.codex,
            event_type="user_prompt",
            actor="human",
            content="before context",
            occurred_at="2026-01-01T00:00:00+00:00",
        )
        add_event(
            self.db_path,
            session_id=self.codex,
            event_type="assistant_message",
            actor="codex",
            content="Cockpit Cloudflare access design",
            occurred_at="2026-01-01T00:01:00+00:00",
        )
        add_event(
            self.db_path,
            session_id=self.codex,
            event_type="session_note",
            actor="human",
            content="after context",
            occurred_at="2026-01-01T00:02:00+00:00",
        )
        add_event(
            self.db_path,
            session_id=self.human,
            event_type="command_result",
            actor="tool",
            content="Cockpit command result",
            occurred_at="2026-01-02T00:00:00+00:00",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_word_source_and_date_filters(self):
        self.assertEqual(len(search_events(self.db_path, "Cockpit", source="codex")), 1)
        self.assertEqual(
            len(search_events(self.db_path, "Cockpit", from_value="2026-01-01", to_value="2026-01-01")),
            1,
        )
        self.assertEqual(len(search_events(self.db_path, "Cockpit", event_type="assistant_message")), 1)
        self.assertEqual(len(search_events(self.db_path, "Cockpit", actor="tool")), 1)

    def test_context_before_after_is_deduplicated(self):
        results = search_events(self.db_path, "Cockpit", context_before=1, context_after=1)
        self.assertEqual([row["sequence_no"] for row in results], [1, 2, 3, 1])
        self.assertEqual(sum(1 for row in results if row["matched"]), 2)
        self.assertEqual(len({row["event_id"] for row in results}), len(results))

    def test_json_cli_output_is_machine_readable(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["--db", str(self.db_path), "search", "Cockpit", "--json"])
        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(len(payload), 2)
        self.assertIn("event_id", payload[0])
        self.assertIn("targets", payload[0])

    def test_mixed_script_substring_fallback(self):
        results = search_events(self.db_path, "Cockpit")
        self.assertEqual(len(results), 2)
