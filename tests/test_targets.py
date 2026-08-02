import tempfile
import unittest
from pathlib import Path

from agent_history.commands.search import search_events
from agent_history.commands.session import create_session
from agent_history.commands.target import add_session_target, add_target, list_targets
from agent_history.commands.event import add_event
from agent_history.db import init_database


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        init_database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_duplicate_target_returns_existing_id(self):
        first = add_target(
            self.db_path,
            target_type="service",
            slug="cloudflare",
            name="Cloudflare",
            locator="cloudflare",
        )
        second = add_target(
            self.db_path,
            target_type="service",
            slug="cloudflare",
            name="A different name",
        )
        self.assertEqual(first, second)
        self.assertEqual(len(list_targets(self.db_path)), 1)

    def test_one_session_can_have_multiple_targets_and_filter_search(self):
        session_id = create_session(self.db_path, source="manual-import")
        cloudflare = add_target(
            self.db_path,
            target_type="service",
            slug="cloudflare",
            name="Cloudflare",
        )
        cockpit = add_target(
            self.db_path,
            target_type="service",
            slug="cockpit",
            name="Cockpit",
        )
        add_session_target(self.db_path, session_id=session_id, target_id=cloudflare, relation_type="configured")
        add_session_target(self.db_path, session_id=session_id, target_id=cockpit, relation_type="configured")
        add_event(
            self.db_path,
            session_id=session_id,
            event_type="assistant_message",
            actor="codex",
            content="Cloudflare Tunnel can expose Cockpit",
        )
        results = search_events(self.db_path, "Cloudflare", target="cloudflare")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["targets"], ["cloudflare", "cockpit"])
