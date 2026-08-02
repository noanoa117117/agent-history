import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path

from agent_history.cli import main
from agent_history.capture.claude_hook import (
    EVENT_MAPPING,
    INSTALLABLE_EVENTS,
    MATCHER_EVENTS,
    SUPPORTED_EVENTS,
    process_hook,
)
from agent_history.commands import CommandError
from agent_history.commands.claude_hook import install, uninstall
from agent_history.commands.search import search_events
from agent_history.db import connect_db, init_database


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "claude_hooks"


class ClaudeHookTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        init_database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def read_fixture(self, name):
        return (FIXTURE_DIR / name).read_bytes()

    def test_core_fixtures_create_session_and_events(self):
        for filename, event_name in (
            ("session_start.json", "SessionStart"),
            ("user_prompt_submit.json", "UserPromptSubmit"),
            ("pre_tool_use_bash.json", "PreToolUse"),
            ("post_tool_use_bash.json", "PostToolUse"),
        ):
            result = process_hook(
                event_name, input_bytes=self.read_fixture(filename), db_path=self.db_path
            )
            self.assertTrue(result.ok, result.error)
        with connect_db(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 4)
            row = connection.execute(
                "SELECT source, source_session_id, model, title FROM sessions"
            ).fetchone()
            self.assertEqual(row["source"], "claude-code")
            self.assertEqual(row["source_session_id"], "claude-fixture-session")
            self.assertEqual(row["model"], "claude-sonnet-4-5")
            self.assertEqual(row["title"], "Claude hook fixture")
        self.assertEqual(len(search_events(self.db_path, "fixture-command")), 2)

    def test_all_local_supported_event_mappings_are_recorded(self):
        payloads = json.loads((FIXTURE_DIR / "all_events.json").read_text(encoding="utf-8"))
        self.assertEqual({item["hook_event_name"] for item in payloads}, set(SUPPORTED_EVENTS))
        for payload in payloads:
            payload["session_id"] = "all-events-session"
            payload["cwd"] = "/home/amida/projects/agent-history"
            payload["transcript_path"] = "/home/amida/.claude/projects/all-events.jsonl"
            # One turn correlator shared by every event, as Claude Code sends it.
            payload["prompt_id"] = "all-events-turn"
            result = process_hook(
                input_bytes=json.dumps(payload).encode("utf-8"), db_path=self.db_path
            )
            self.assertTrue(result.ok, (payload["hook_event_name"], result.error))
        with connect_db(self.db_path) as connection:
            rows = connection.execute(
                "SELECT event_type, actor FROM events ORDER BY sequence_no"
            ).fetchall()
        self.assertEqual(len(rows), len(SUPPORTED_EVENTS))
        self.assertEqual(
            {(row["event_type"], row["actor"]) for row in rows},
            set(EVENT_MAPPING.values()),
        )

    def test_session_start_is_idempotent_and_session_end_completes(self):
        payload = json.loads((FIXTURE_DIR / "session_start.json").read_text(encoding="utf-8"))
        first = process_hook(input_bytes=json.dumps(payload).encode(), db_path=self.db_path)
        second = process_hook(input_bytes=json.dumps(payload).encode(), db_path=self.db_path)
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertTrue(second.duplicate)
        end_payload = {
            "session_id": payload["session_id"],
            "hook_event_name": "SessionEnd",
            "reason": "other",
            "cwd": payload["cwd"],
        }
        self.assertTrue(process_hook(input_bytes=json.dumps(end_payload).encode(), db_path=self.db_path).ok)
        with connect_db(self.db_path) as connection:
            session = connection.execute("SELECT * FROM sessions").fetchone()
            self.assertEqual(session["capture_status"], "completed")
            self.assertIsNotNone(session["ended_at"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 2)

    def test_missing_session_start_is_automatically_created(self):
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "implicit-session",
            "cwd": "/tmp",
            "tool_name": "Read",
            "tool_use_id": "implicit-tool",
            "tool_response": "fixture result",
        }
        result = process_hook(input_bytes=json.dumps(payload).encode(), db_path=self.db_path)
        self.assertTrue(result.ok, result.error)
        with connect_db(self.db_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT source_session_id FROM sessions"
                ).fetchone()[0],
                "implicit-session",
            )

    def test_nested_secret_values_are_sanitized_before_event_and_dead_letter(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "secret-session",
            "cwd": "/tmp",
            "tool_name": "Bash",
            "tool_use_id": "secret-tool",
            "tool_input": {
                "command": "curl https://example.com?token=abc123",
                "env": {"GITHUB_TOKEN": "real-fixture-token", "nested": [{"password": "pw"}]},
                "headers": {"Authorization": "Bearer real-bearer"},
            },
        }
        result = process_hook(input_bytes=json.dumps(payload).encode(), db_path=self.db_path)
        self.assertTrue(result.ok, result.error)
        with connect_db(self.db_path) as connection:
            row = connection.execute("SELECT content, content_json, sensitivity FROM events").fetchone()
        self.assertEqual(row["sensitivity"], "sanitized")
        self.assertNotIn("real-fixture-token", row["content_json"])
        self.assertNotIn("real-bearer", row["content_json"])
        self.assertNotIn("abc123", row["content_json"])

    def test_size_limits_record_metadata_and_do_not_store_binary_body(self):
        old_content = os.environ.get("AGENT_HISTORY_HOOK_MAX_CONTENT_BYTES")
        old_json = os.environ.get("AGENT_HISTORY_HOOK_MAX_JSON_BYTES")
        os.environ["AGENT_HISTORY_HOOK_MAX_CONTENT_BYTES"] = "256"
        os.environ["AGENT_HISTORY_HOOK_MAX_JSON_BYTES"] = "2048"
        try:
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "large-session",
                "cwd": "/tmp",
                "prompt": "x" * 5000,
                "image_data": "A" * 4096,
            }
            result = process_hook(input_bytes=json.dumps(payload).encode(), db_path=self.db_path)
            self.assertTrue(result.ok, result.error)
            with connect_db(self.db_path) as connection:
                row = connection.execute(
                    "SELECT content, content_json, payload_size, truncated FROM events"
                ).fetchone()
            self.assertLessEqual(len(row["content"].encode()), 256)
            self.assertLessEqual(len(row["content_json"].encode()), 2048)
            self.assertGreater(row["payload_size"], 0)
            self.assertEqual(row["truncated"], 1)
            self.assertNotIn("A" * 1024, row["content_json"])
            structured = json.loads(row["content_json"])
            self.assertGreater(structured["original_size"], structured["stored_size"])
            self.assertEqual(structured["stored_size"], len(row["content_json"].encode()))
        finally:
            if old_content is None:
                os.environ.pop("AGENT_HISTORY_HOOK_MAX_CONTENT_BYTES", None)
            else:
                os.environ["AGENT_HISTORY_HOOK_MAX_CONTENT_BYTES"] = old_content
            if old_json is None:
                os.environ.pop("AGENT_HISTORY_HOOK_MAX_JSON_BYTES", None)
            else:
                os.environ["AGENT_HISTORY_HOOK_MAX_JSON_BYTES"] = old_json

    def test_invalid_unknown_and_missing_input_are_dead_lettered_without_failure_code(self):
        invalid = process_hook("SessionStart", input_bytes=b"{not-json", db_path=self.db_path)
        unknown = process_hook(
            input_bytes=json.dumps(
                {"hook_event_name": "FutureEvent", "session_id": "unknown"}
            ).encode(),
            db_path=self.db_path,
        )
        missing = process_hook(
            input_bytes=json.dumps({"hook_event_name": "SessionStart"}).encode(),
            db_path=self.db_path,
        )
        self.assertFalse(invalid.ok)
        self.assertFalse(unknown.ok)
        self.assertFalse(missing.ok)
        self.assertTrue(invalid.dead_letter_path.exists())
        self.assertTrue(unknown.dead_letter_path.exists())
        self.assertTrue(missing.dead_letter_path.exists())
        with connect_db(self.db_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_invalid_utf8_and_dead_letter_payload_are_safe(self):
        invalid = process_hook("SessionStart", input_bytes=b"\xff\xfe", db_path=self.db_path)
        self.assertFalse(invalid.ok)
        self.assertIsNotNone(invalid.dead_letter_path)
        self.assertTrue(invalid.dead_letter_path.exists())

        unsafe = {
            "hook_event_name": "FutureEvent",
            "session_id": "dead-letter-session",
            "nested": {"GITHUB_TOKEN": "real-fixture-token"},
        }
        result = process_hook(
            input_bytes=json.dumps(unsafe).encode("utf-8"), db_path=self.db_path
        )
        self.assertFalse(result.ok)
        dead_letter = result.dead_letter_path.read_text(encoding="utf-8")
        self.assertNotIn("real-fixture-token", dead_letter)
        self.assertIn("<REDACTED_SECRET>", dead_letter)

    def test_uninitialized_database_is_dead_lettered_without_creating_database(self):
        uninitialized = Path(self.temp_dir.name) / "not-initialized.db"
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "uninitialized-session",
        }
        result = process_hook(
            input_bytes=json.dumps(payload).encode("utf-8"), db_path=uninitialized
        )
        self.assertFalse(result.ok)
        self.assertFalse(uninitialized.exists())
        self.assertTrue(result.dead_letter_path.exists())

    def test_concurrent_hook_writes_keep_unique_sequence_numbers(self):
        start = {
            "hook_event_name": "SessionStart",
            "session_id": "concurrent-session",
            "cwd": "/tmp",
            "source": "startup",
        }
        self.assertTrue(process_hook(input_bytes=json.dumps(start).encode(), db_path=self.db_path).ok)

        def submit(index):
            payload = {
                "hook_event_name": "PreToolUse",
                "session_id": "concurrent-session",
                "cwd": "/tmp",
                "tool_name": "Bash",
                "tool_use_id": f"concurrent-tool-{index}",
                "tool_input": {"command": f"printf {index}"},
            }
            return process_hook(input_bytes=json.dumps(payload).encode(), db_path=self.db_path)

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(submit, range(24)))
        self.assertTrue(all(result.ok for result in results))
        with connect_db(self.db_path) as connection:
            sequences = [
                row[0]
                for row in connection.execute(
                    "SELECT sequence_no FROM events ORDER BY sequence_no"
                ).fetchall()
            ]
        self.assertEqual(sequences, list(range(1, 26)))

    def _fts_event_ids(self, term):
        """Query the FTS index directly.

        search_events() also has a LIKE fallback, so asserting through it
        cannot prove hook-captured content reached the FTS index.
        """

        with connect_db(self.db_path) as connection:
            rows = connection.execute(
                "SELECT event_id FROM events_fts WHERE events_fts MATCH ?",
                ('"' + term + '"',),
            ).fetchall()
        return [int(row["event_id"]) for row in rows]

    def test_hook_captured_content_reaches_the_fts_index(self):
        result = process_hook(
            input_bytes=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "fts-session",
                    "cwd": "/tmp",
                    "prompt_id": "turn-1",
                    "prompt": "investigate cockpit deployment",
                }
            ).encode(),
            db_path=self.db_path,
        )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self._fts_event_ids("cockpit"), [result.event_id])
        with connect_db(self.db_path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0], 1
            )

    def test_turn_correlator_does_not_collapse_distinct_events(self):
        """prompt_id is shared by every event of a turn, so it is not an event id."""

        base = {
            "session_id": "turn-session",
            "cwd": "/tmp",
            "transcript_path": "/home/amida/.claude/projects/x.jsonl",
            "prompt_id": "one-turn-for-all-of-these",
        }
        payloads = [
            {"hook_event_name": "FileChanged", "file_path": "/tmp/a.py", "event": "change"},
            {"hook_event_name": "FileChanged", "file_path": "/tmp/b.py", "event": "change"},
            {"hook_event_name": "Notification", "message": "first", "notification_type": "idle_prompt"},
            {"hook_event_name": "Notification", "message": "second", "notification_type": "idle_prompt"},
            {"hook_event_name": "SubagentStart", "agent_id": "agent-1", "agent_type": "Explore"},
            {"hook_event_name": "SubagentStart", "agent_id": "agent-2", "agent_type": "Explore"},
            {"hook_event_name": "TaskCreated", "task_id": "task-1", "task_subject": "one"},
            {"hook_event_name": "TaskCreated", "task_id": "task-2", "task_subject": "two"},
        ]
        for extra in payloads:
            payload = dict(base)
            payload.update(extra)
            result = process_hook(input_bytes=json.dumps(payload).encode(), db_path=self.db_path)
            self.assertTrue(result.ok, result.error)
            self.assertFalse(result.duplicate, f"wrongly deduplicated: {extra}")
        with connect_db(self.db_path) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM events").fetchone()[0], len(payloads)
            )

    def test_identical_redelivery_of_the_same_hook_is_deduplicated(self):
        """The same hook registered in two settings scopes delivers twice."""

        payload = {
            "hook_event_name": "Notification",
            "session_id": "redeliver-session",
            "cwd": "/tmp",
            "prompt_id": "turn-1",
            "message": "same notification",
            "notification_type": "idle_prompt",
        }
        first = process_hook(input_bytes=json.dumps(payload).encode(), db_path=self.db_path)
        second = process_hook(input_bytes=json.dumps(payload).encode(), db_path=self.db_path)
        self.assertFalse(first.duplicate)
        self.assertTrue(second.duplicate)
        self.assertEqual(first.event_id, second.event_id)

    def test_repeated_identical_tool_calls_stay_distinct(self):
        base = {
            "hook_event_name": "PreToolUse",
            "session_id": "repeat-session",
            "cwd": "/tmp",
            "prompt_id": "turn-1",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
        }
        ids = set()
        for tool_use_id in ("toolu-1", "toolu-2"):
            payload = dict(base, tool_use_id=tool_use_id)
            result = process_hook(input_bytes=json.dumps(payload).encode(), db_path=self.db_path)
            self.assertTrue(result.ok, result.error)
            self.assertFalse(result.duplicate)
            ids.add(result.event_id)
        self.assertEqual(len(ids), 2)

    def test_long_prose_is_not_discarded_as_binary(self):
        prose = ("the quick brown fox jumps over the lazy dog " * 40)[:2048]
        self.assertEqual(len(prose) % 4, 0, "keep the length divisible by four")
        result = process_hook(
            input_bytes=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "prose-session",
                    "cwd": "/tmp",
                    "prompt_id": "turn-1",
                    "prompt": prose,
                }
            ).encode(),
            db_path=self.db_path,
        )
        self.assertTrue(result.ok, result.error)
        with connect_db(self.db_path) as connection:
            content = connection.execute("SELECT content FROM events").fetchone()["content"]
        self.assertNotIn("<REDACTED_BINARY>", content)
        self.assertIn("quick brown fox", content)

    def test_transcript_path_is_home_normalized_in_events_and_dead_letters(self):
        home_path = str(Path.home() / ".claude" / "projects" / "private-project" / "t.jsonl")
        stored = process_hook(
            input_bytes=json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "path-session",
                    "cwd": "/tmp",
                    "transcript_path": home_path,
                    "prompt": "normalize me",
                }
            ).encode(),
            db_path=self.db_path,
        )
        self.assertTrue(stored.ok, stored.error)
        with connect_db(self.db_path) as connection:
            payload = json.loads(
                connection.execute("SELECT content_json FROM events").fetchone()["content_json"]
            )
        self.assertTrue(payload["transcript_path"].startswith("~/"))

        failed = process_hook(
            input_bytes=json.dumps(
                {"hook_event_name": "FutureEvent", "session_id": "x", "transcript_path": home_path}
            ).encode(),
            db_path=self.db_path,
        )
        dead_letter = failed.dead_letter_path.read_text(encoding="utf-8")
        self.assertNotIn(str(Path.home()), dead_letter)
        self.assertIn("~/.claude", dead_letter)

    def test_install_uninstall_is_dry_run_safe_idempotent_and_preserves_existing_hooks(self):
        settings = Path(self.temp_dir.name) / "settings.json"
        original = {
            "permissions": {"allow": ["Bash(true)"]},
            "hooks": {
                "Notification": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "existing-hook"}]}
                ]
            },
        }
        settings.write_text(json.dumps(original), encoding="utf-8")
        dry = install(path=settings)
        self.assertIn("No files changed", dry)
        self.assertEqual(json.loads(settings.read_text()), original)
        applied = install(path=settings, apply=True)
        self.assertIn("Changed: yes", applied)
        merged = json.loads(settings.read_text())
        self.assertEqual(merged["permissions"], original["permissions"])
        self.assertIn("existing-hook", json.dumps(merged))
        self.assertIn("agent-history-claude-hook", json.dumps(merged))
        self.assertNotIn("WorktreeCreate", merged["hooks"])
        second = install(path=settings, apply=True)
        self.assertIn("Changed: no", second)
        uninstall_dry = uninstall(path=settings)
        self.assertIn("No files changed", uninstall_dry)
        uninstall(path=settings, apply=True)
        cleaned = json.loads(settings.read_text())
        self.assertIn("existing-hook", json.dumps(cleaned))
        self.assertNotIn("agent-history-claude-hook", json.dumps(cleaned))
        self.assertTrue(list(self.temp_dir_path().glob("settings.json.bak-*")))

    def temp_dir_path(self):
        return Path(self.temp_dir.name)

    def test_event_and_matcher_sets_match_the_claude_code_hook_contract(self):
        """Pinned to the event lists in the Claude Code 2.1.220 binary.

        Registering a name Claude does not know is silently ignored by Claude,
        so drift here means events stop being captured with no error anywhere.
        """

        claude_hook_events = {
            "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch",
            "Notification", "UserPromptSubmit", "UserPromptExpansion", "SessionStart",
            "SessionEnd", "Stop", "StopFailure", "SubagentStart", "SubagentStop",
            "PreCompact", "PostCompact", "PermissionRequest", "PermissionDenied",
            "Setup", "TeammateIdle", "TaskCreated", "TaskCompleted", "Elicitation",
            "ElicitationResult", "ConfigChange", "WorktreeCreate", "WorktreeRemove",
            "InstructionsLoaded", "CwdChanged", "FileChanged", "DirectoryAdded",
            "MessageDisplay",
        }
        claude_matcher_events = {
            "PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest",
            "PermissionDenied", "UserPromptExpansion", "SessionStart", "SessionEnd",
            "Setup", "PreCompact", "PostCompact", "Notification", "SubagentStart",
            "SubagentStop", "Elicitation", "ElicitationResult", "ConfigChange",
            "InstructionsLoaded", "DirectoryAdded",
        }
        self.assertEqual(set(SUPPORTED_EVENTS), claude_hook_events)
        self.assertEqual(set(EVENT_MAPPING), claude_hook_events)
        self.assertEqual(MATCHER_EVENTS, claude_matcher_events)
        self.assertNotIn("WorktreeCreate", INSTALLABLE_EVENTS)

    def test_example_config_matches_what_the_installer_writes(self):
        example = json.loads(
            (Path(__file__).resolve().parents[1] / "config" / "claude-hooks.example.json")
            .read_text(encoding="utf-8")
        )
        hooks = example["hooks"]
        self.assertEqual(set(hooks), set(INSTALLABLE_EVENTS))
        for event_name, groups in hooks.items():
            with self.subTest(event=event_name):
                self.assertEqual("matcher" in groups[0], event_name in MATCHER_EVENTS)

    def test_global_db_option_reaches_the_claude_subcommands(self):
        """A subparser --db would shadow the global one and silently use the default DB."""

        process_hook(
            input_bytes=json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "cli-routed-session",
                    "cwd": "/tmp",
                    "source": "startup",
                }
            ).encode(),
            db_path=self.db_path,
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["--db", str(self.db_path), "claude-sessions"])
        self.assertEqual(exit_code, 0)
        self.assertIn("claude-code", stdout.getvalue())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(main(["--db", str(self.db_path), "claude-hook-status"]), 0)
        self.assertIn(str(self.db_path), stdout.getvalue())

    def test_invalid_existing_settings_are_not_modified(self):
        settings = Path(self.temp_dir.name) / "invalid.json"
        settings.write_text("{invalid", encoding="utf-8")
        with self.assertRaises(CommandError):
            install(path=settings, apply=True)
        self.assertEqual(settings.read_text(), "{invalid")
