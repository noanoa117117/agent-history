import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_history.capture import hook_fast, spool
from agent_history.db import connect_db, init_database
from agent_history.worker.runner import Worker


class CodexHookTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        self.spool_root = os.path.join(self.temp_dir.name, "spool")
        os.environ["AGENT_HISTORY_SPOOL_DIR"] = self.spool_root
        init_database(self.db_path)
        spool.ensure_dirs(self.spool_root)

    def tearDown(self):
        os.environ.pop("AGENT_HISTORY_SPOOL_DIR", None)
        self.temp_dir.cleanup()

    def _spool(self, event_name, **payload):
        payload.setdefault("session_id", "codex-session")
        payload.setdefault("hook_event_name", event_name)
        payload.setdefault("cwd", "/workspace/agent-history")
        return hook_fast.run(
            [event_name, "codex"], stdin=io.BytesIO(json.dumps(payload).encode())
        )

    def test_codex_lifecycle_uses_shared_spool_and_normalized_events(self):
        self._spool("SessionStart", model="gpt-test", source="startup")
        # A duplicated delivery (for example, after a worker crash before
        # unlink) must be absorbed by the existing session/dedup-key index.
        self._spool("SessionStart", model="gpt-test", source="startup")
        self._spool("UserPromptSubmit", turn_id="turn-1", prompt="hello@corp.internal")
        self._spool(
            "Stop",
            turn_id="turn-1",
            last_assistant_message="done with ghp_abcdefghijklmnopqrstuvwxyz1234567890",
        )
        self._spool(
            "SessionEnd",
            reason="other",
            transcript_path="/home/amida/.codex/sessions/private.jsonl",
        )

        result = Worker(self.db_path).run(drain_only=True)
        self.assertEqual(result.inserted, 4)

        with connect_db(self.db_path) as connection:
            session = connection.execute(
                "SELECT source, source_session_id, model, initial_cwd FROM sessions"
            ).fetchone()
            self.assertEqual(tuple(session), ("codex", "codex-session", "gpt-test", "/workspace/agent-history"))
            events = connection.execute(
                "SELECT event_type, actor, content, content_json FROM events ORDER BY sequence_no"
            ).fetchall()

        self.assertEqual(
            [(row["event_type"], row["actor"]) for row in events],
            [
                ("session_start", "system"),
                ("user_prompt", "human"),
                ("assistant_stop", "codex"),
                ("session_end", "system"),
            ],
        )
        self.assertIn("<REDACTED_EMAIL>", events[1]["content"])
        self.assertIn("<REDACTED_SECRET>", events[2]["content"])
        self.assertNotIn("transcript_path", events[3]["content_json"])

    def test_codex_spool_header_marks_source_without_parsing_stdin(self):
        path = self._spool("SessionStart", model="gpt-test")
        header, _ = spool.split_file(Path(path).read_bytes())
        self.assertEqual(json.loads(header)["src"], "codex")
