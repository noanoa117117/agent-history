import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from agent_history.capture import hook_fast, spool
from agent_history.commands import CommandError
from agent_history.commands import worker as worker_commands
from agent_history.db import connect_db, init_database
from agent_history.presets import (
    BALANCED_EVENTS,
    FULL_EVENTS,
    HIGH_FREQUENCY_EVENTS,
    PRESETS,
    preset_events,
)
from agent_history.capture.claude_hook import INSTALLABLE_EVENTS, SUPPORTED_EVENTS
from agent_history.worker.ingest import PermanentError, parse_spool_file
from agent_history.worker.runner import Worker, WorkerBusyError, acquire_lock, release_lock


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        self.root = os.path.join(self.temp_dir.name, "spool")
        init_database(self.db_path)
        os.environ["AGENT_HISTORY_SPOOL_DIR"] = self.root
        spool.ensure_dirs(self.root)

    def tearDown(self):
        os.environ.pop("AGENT_HISTORY_SPOOL_DIR", None)
        self.temp_dir.cleanup()

    def worker(self, **kwargs):
        return Worker(self.db_path, **kwargs)

    def spool_event(self, event_name="Stop", session_id="w-1", **extra):
        payload = dict(
            {"session_id": session_id, "cwd": "/tmp", "hook_event_name": event_name}, **extra
        )
        return hook_fast.run([event_name], stdin=io.BytesIO(json.dumps(payload).encode()))

    def pending(self):
        return sorted(os.listdir(spool.subdir(self.root, spool.DIR_PENDING)))

    def failed(self):
        return sorted(
            name
            for name in os.listdir(spool.subdir(self.root, spool.DIR_FAILED))
            if name.endswith(spool.FILE_SUFFIX)
        )

    def rows(self, query, params=()):
        with connect_db(self.db_path) as connection:
            return connection.execute(query, params).fetchall()


class IngestTests(WorkerTestCase):
    def test_drain_ingests_all_events(self):
        for index in range(5):
            self.spool_event("UserPromptSubmit", prompt=f"prompt {index}")
        totals = self.worker().run(drain_only=True)
        self.assertEqual(totals.inserted, 5)
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.rows("SELECT id FROM events")), 5)

    def test_batch_commits_are_amortised(self):
        """100 events must not cost 100 transactions -- that was the bug."""

        for index in range(100):
            self.spool_event("UserPromptSubmit", prompt=f"p{index}")

        statements = []
        worker = self.worker(batch_size=50)
        original = connect_db

        import agent_history.worker.runner as runner

        class Tracing:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                connection = self.inner.__enter__()
                connection.set_trace_callback(statements.append)
                return connection

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

        runner.connect_db = lambda *a, **k: Tracing(original(*a, **k))
        try:
            totals = worker.run(drain_only=True)
        finally:
            runner.connect_db = original

        self.assertEqual(totals.inserted, 100)
        transactions = sum(1 for line in statements if line.strip().startswith("BEGIN IMMEDIATE"))
        self.assertGreater(transactions, 0)
        self.assertLess(
            transactions, 100, f"expected batched commits, got {transactions} transactions"
        )
        self.assertLessEqual(transactions, 4)

    def test_reprocessing_the_same_event_does_not_duplicate(self):
        path = self.spool_event("SessionStart", source="startup")
        saved = Path(path).read_bytes()
        self.worker().run(drain_only=True)
        self.assertEqual(len(self.rows("SELECT id FROM events")), 1)

        # Simulate a crash between commit and unlink: the file is still pending.
        Path(spool.subdir(self.root, spool.DIR_PENDING), os.path.basename(path)).write_bytes(saved)
        self.worker().run(drain_only=True)
        self.assertEqual(len(self.rows("SELECT id FROM events")), 1)

    def test_sequence_numbers_follow_spool_order(self):
        for index in range(6):
            self.spool_event("UserPromptSubmit", prompt=f"p{index}")
        order = [name.split("-")[0] for name in self.pending()]
        self.assertEqual(order, sorted(order))
        self.worker().run(drain_only=True)
        rows = self.rows("SELECT sequence_no, content FROM events ORDER BY sequence_no")
        self.assertEqual([row["sequence_no"] for row in rows], [1, 2, 3, 4, 5, 6])
        self.assertEqual(
            [row["content"] for row in rows], [f"p{index}" for index in range(6)]
        )

    def test_session_end_closes_the_session(self):
        self.spool_event("SessionStart", source="startup")
        self.spool_event("SessionEnd", reason="exit")
        self.worker().run(drain_only=True)
        row = self.rows("SELECT ended_at, capture_status FROM sessions")[0]
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["capture_status"], "completed")

    def test_fts_index_stays_consistent(self):
        for index in range(10):
            self.spool_event("UserPromptSubmit", prompt=f"searchable{index}")
        self.worker().run(drain_only=True)
        with connect_db(self.db_path) as connection:
            events = connection.execute("SELECT count(*) AS n FROM events").fetchone()["n"]
            indexed = connection.execute("SELECT count(*) AS n FROM events_fts").fetchone()["n"]
            self.assertEqual(events, indexed)
            # Match the FTS table directly: going through search_events() would
            # pass on the LIKE fallback even with a broken index.
            hits = connection.execute(
                "SELECT event_id FROM events_fts WHERE events_fts MATCH ?", ("searchable3",)
            ).fetchall()
            self.assertEqual(len(hits), 1)


class QuarantineTests(WorkerTestCase):
    def write_raw(self, name, content):
        path = Path(spool.subdir(self.root, spool.DIR_PENDING), name)
        path.write_bytes(content)
        return path

    def test_malformed_json_is_quarantined_without_stalling_the_batch(self):
        self.spool_event("UserPromptSubmit", prompt="good one")
        header = spool.build_header(spool.new_uid(), "Stop", 5)
        self.write_raw(
            f"{spool.new_uid()}-Stop{spool.FILE_SUFFIX}",
            header.encode() + b"\nNOT JSON",
        )
        totals = self.worker().run(drain_only=True)
        self.assertEqual(totals.inserted, 1)
        self.assertEqual(totals.quarantined, 1)
        self.assertEqual(self.pending(), [])
        self.assertEqual(len(self.failed()), 1)
        reason = Path(
            spool.subdir(self.root, spool.DIR_FAILED), self.failed()[0] + ".reason"
        ).read_text()
        self.assertIn("invalid hook JSON", reason)

    def test_unknown_schema_version_is_quarantined(self):
        uid = spool.new_uid()
        header = '{"v":99,"uid":"%s","ev":"Stop","ts_ns":1,"pid":1,"size":2,"truncated":false}' % uid
        self.write_raw(f"{uid}-Stop{spool.FILE_SUFFIX}", header.encode() + b"\n{}")
        totals = self.worker().run(drain_only=True)
        self.assertEqual(totals.quarantined, 1)
        self.assertEqual(len(self.failed()), 1)

    def test_missing_session_id_is_quarantined(self):
        path = self.spool_event("Stop")
        raw = Path(path).read_bytes()
        header, _ = spool.split_file(raw)
        Path(path).write_bytes(header + b'\n{"hook_event_name":"Stop"}')
        totals = self.worker().run(drain_only=True)
        self.assertEqual(totals.quarantined, 1)

    def test_event_name_mismatch_is_rejected(self):
        path = self.spool_event("Stop")
        raw = Path(path).read_bytes()
        header, _ = spool.split_file(raw)
        Path(path).write_bytes(
            header + b'\n{"session_id":"x","hook_event_name":"SessionStart"}'
        )
        with self.assertRaises(PermanentError):
            parse_spool_file(str(path))

    def test_worker_ignores_unpublished_tmp_files(self):
        Path(spool.subdir(self.root, spool.DIR_TMP), "half-written.spool").write_bytes(
            b'{"v":1,"uid":"x"'
        )
        self.spool_event("UserPromptSubmit", prompt="only me")
        totals = self.worker().run(drain_only=True)
        self.assertEqual(totals.inserted, 1)
        self.assertEqual(totals.quarantined, 0)
        self.assertEqual(len(os.listdir(spool.subdir(self.root, spool.DIR_TMP))), 1)


class LockTests(WorkerTestCase):
    def test_only_one_worker_may_run(self):
        lock = acquire_lock(self.root)
        try:
            with self.assertRaises(WorkerBusyError):
                self.worker().run(drain_only=True)
        finally:
            release_lock(lock)
        # Released: a worker can run again.
        self.spool_event("Stop")
        self.assertEqual(self.worker().run(drain_only=True).inserted, 1)

    def test_status_reports_pending_and_failed(self):
        self.spool_event("Stop")
        output = worker_commands.spool_status(db_path=self.db_path)
        self.assertIn("Pending: 1 files", output)
        output = worker_commands.worker_status(db_path=self.db_path)
        self.assertIn("Worker: not running", output)


class InterruptionTests(WorkerTestCase):
    def test_events_survive_a_stopped_worker(self):
        """Spooling works with no worker running; nothing is lost."""

        for index in range(20):
            self.spool_event("UserPromptSubmit", prompt=f"p{index}")
        self.assertEqual(len(self.pending()), 20)
        self.assertEqual(len(self.rows("SELECT id FROM events")), 0)

        # A worker that handles only part of the backlog, then stops.
        worker = self.worker(batch_size=5)
        with connect_db(self.db_path) as connection:
            worker.process_batch(connection)
        self.assertEqual(len(self.pending()), 15)

        # A later worker picks up exactly the remainder.
        self.assertEqual(self.worker().run(drain_only=True).inserted, 15)
        self.assertEqual(len(self.rows("SELECT id FROM events")), 20)
        contents = {row["content"] for row in self.rows("SELECT content FROM events")}
        self.assertEqual(contents, {f"p{index}" for index in range(20)})


class ExistingDatabaseTests(WorkerTestCase):
    def test_existing_rows_are_untouched(self):
        with connect_db(self.db_path) as connection:
            with connection:
                connection.execute(
                    "INSERT INTO sessions (id, source, started_at, created_at) "
                    "VALUES ('legacy', 'manual', '2026-01-01', '2026-01-01')"
                )
                for index in range(6):
                    connection.execute(
                        "INSERT INTO events (session_id, sequence_no, event_type, actor, "
                        "content, occurred_at, created_at) VALUES (?, ?, 'note', 'human', ?, ?, ?)",
                        ("legacy", index + 1, f"legacy {index}", "2026-01-01", "2026-01-01"),
                    )
        before = self.rows("SELECT id, session_id, sequence_no, content FROM events ORDER BY id")

        self.spool_event("UserPromptSubmit", prompt="new event")
        self.worker().run(drain_only=True)

        after = self.rows(
            "SELECT id, session_id, sequence_no, content FROM events "
            "WHERE session_id = 'legacy' ORDER BY id"
        )
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])
        self.assertEqual(len(self.rows("SELECT id FROM events")), 7)
        with connect_db(self.db_path) as connection:
            events = connection.execute("SELECT count(*) AS n FROM events").fetchone()["n"]
            indexed = connection.execute("SELECT count(*) AS n FROM events_fts").fetchone()["n"]
            self.assertEqual(events, indexed)


class PresetTests(unittest.TestCase):
    def test_presets_are_nested(self):
        self.assertLess(set(PRESETS["minimal"]), set(PRESETS["balanced"]))
        self.assertLess(set(PRESETS["balanced"]), set(PRESETS["full"]))

    def test_high_frequency_events_are_full_only(self):
        for event in HIGH_FREQUENCY_EVENTS:
            self.assertNotIn(event, PRESETS["minimal"])
            self.assertNotIn(event, BALANCED_EVENTS)
            self.assertIn(event, FULL_EVENTS)

    def test_worktree_create_is_never_installed(self):
        for events in PRESETS.values():
            self.assertNotIn("WorktreeCreate", events)

    def test_full_matches_installable_events(self):
        self.assertEqual(set(FULL_EVENTS), set(INSTALLABLE_EVENTS))

    def test_presets_keep_canonical_order(self):
        for events in PRESETS.values():
            ordered = [event for event in SUPPORTED_EVENTS if event in set(events)]
            self.assertEqual(list(events), ordered)

    def test_unknown_preset_is_rejected(self):
        with self.assertRaises(ValueError):
            preset_events("enormous")

    def test_shipped_configs_match_the_presets(self):
        """The committed examples are generated; keep them from drifting."""

        config_dir = Path(__file__).resolve().parents[1] / "config"
        for name, events in PRESETS.items():
            document = json.loads((config_dir / f"claude-hooks.{name}.json").read_text())
            self.assertEqual(list(document["hooks"]), list(events), f"{name} preset drifted")
        example = json.loads((config_dir / "claude-hooks.example.json").read_text())
        self.assertEqual(
            list(example["hooks"]), list(BALANCED_EVENTS), "example config must be `balanced`"
        )

    def test_registered_commands_carry_their_event_name(self):
        """The hook reads its event from argv because it cannot parse JSON."""

        config_dir = Path(__file__).resolve().parents[1] / "config"
        document = json.loads((config_dir / "claude-hooks.balanced.json").read_text())
        for event_name, groups in document["hooks"].items():
            for group in groups:
                for handler in group["hooks"]:
                    self.assertTrue(
                        handler["command"].endswith(" " + event_name),
                        f"{event_name} command must end with its event name",
                    )


class DatabaseReadinessTests(WorkerTestCase):
    def test_initializes_a_missing_database(self):
        fresh = Path(self.temp_dir.name) / "fresh.db"
        self.assertFalse(fresh.exists())
        worker_commands.ensure_database_ready(fresh, timeout=10, log=lambda _m: None)
        self.assertTrue(fresh.exists())
        with connect_db(fresh) as connection:
            names = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master")
            }
        self.assertIn("events", names)
        self.assertIn("events_fts", names)

    def test_does_not_reinitialize_a_ready_database(self):
        """init rebuilds the FTS index; it must not run on every restart."""

        calls = []
        original = worker_commands.init_database
        worker_commands.init_database = lambda *a, **k: calls.append(a) or original(*a, **k)
        try:
            worker_commands.ensure_database_ready(
                self.db_path, timeout=10, log=lambda _m: None
            )
        finally:
            worker_commands.init_database = original
        self.assertEqual(calls, [])

    def test_gives_up_with_a_reason_instead_of_looping(self):
        unusable = Path(self.temp_dir.name) / "no-such-dir" / "sub" / "x.db"
        os.makedirs(unusable.parent.parent, exist_ok=True)
        os.chmod(unusable.parent.parent, 0o500)
        started = time.monotonic()
        try:
            with self.assertRaises(CommandError) as caught:
                worker_commands.ensure_database_ready(
                    unusable, timeout=2, log=lambda _m: None
                )
        finally:
            os.chmod(unusable.parent.parent, 0o700)
        self.assertIn("not ready after", str(caught.exception))
        self.assertLess(time.monotonic() - started, 20)


class ForegroundWorkerTests(WorkerTestCase):
    def test_lock_wait_does_not_fail_immediately(self):
        """The service waits for the lock; exiting would be a restart loop."""

        worker = self.worker()
        lock = acquire_lock(self.root)
        try:
            worker.request_stop()  # so the wait loop gives up promptly
            with self.assertRaises(WorkerBusyError):
                worker.acquire(wait=True, wait_interval=0.01)
        finally:
            release_lock(lock)
        # Lock free again: acquiring now succeeds.
        release_lock(worker.acquire(wait=True, wait_interval=0.01))

    def test_one_shot_start_still_reports_a_busy_lock(self):
        lock = acquire_lock(self.root)
        try:
            with self.assertRaises(WorkerBusyError):
                self.worker().run(drain_only=True)
        finally:
            release_lock(lock)

    def test_stop_finishes_the_batch_without_draining_the_backlog(self):
        for index in range(30):
            self.spool_event("UserPromptSubmit", prompt=f"p{index}")
        worker = self.worker(batch_size=5)
        messages = []
        worker.logger = messages.append

        original = worker.process_batch

        def stop_after_first(connection):
            result = original(connection)
            worker.request_stop()
            return result

        worker.process_batch = stop_after_first
        totals = worker.run()
        self.assertEqual(totals.inserted, 5)
        # The remaining 25 stay on disk for the next run rather than blocking
        # shutdown past Docker's grace period.
        self.assertEqual(len(self.pending()), 25)
        self.assertTrue(any("stop requested" in m for m in messages))


class ComposeWorkerServiceTests(unittest.TestCase):
    """The worker service contract lives in compose.yaml, so assert it there."""

    @classmethod
    def setUpClass(cls):
        try:
            import yaml
        except ImportError:  # pragma: no cover - container image has no PyYAML
            raise unittest.SkipTest("PyYAML is not installed")
        path = Path(__file__).resolve().parents[1] / "compose.yaml"
        cls.compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        cls.worker = cls.compose["services"]["agent-history-worker"]

    def test_runs_in_the_foreground(self):
        command = self.worker["command"]
        joined = " ".join(command) if isinstance(command, list) else str(command)
        self.assertIn("agent-history-worker-run", joined)
        self.assertNotIn("--detach", joined)
        self.assertNotIn("worker-start", joined)

    def test_resource_limits(self):
        self.assertEqual(self.worker["mem_limit"], "512m")
        self.assertEqual(self.worker["mem_reservation"], "256m")
        self.assertEqual(self.worker["memswap_limit"], "512m")
        self.assertEqual(str(self.worker["cpus"]), "0.5")
        self.assertEqual(self.worker["pids_limit"], 64)
        self.assertEqual(self.worker["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", self.worker["security_opt"])
        self.assertTrue(self.worker["init"])
        self.assertEqual(self.worker["restart"], "unless-stopped")
        self.assertEqual(self.worker["logging"]["options"]["max-size"], "10m")
        self.assertEqual(self.worker["logging"]["options"]["max-file"], "3")

    def test_volume_wiring(self):
        mounts = self.worker["volumes"]
        self.assertIn("agent-history-workspace:/workspace/agent-history:ro", mounts)
        self.assertIn("agent-history-data:/workspace/agent-history/data", mounts)
        joined = " ".join(mounts)
        for secret_volume in (
            "agent-history-claude-home",
            "agent-history-codex-home",
            "agent-history-github-auth",
        ):
            self.assertNotIn(secret_volume, joined, "worker needs no credentials")

    def test_shares_the_dev_image_and_defines_no_new_volumes(self):
        dev = self.compose["services"]["agent-history-dev"]
        self.assertEqual(self.worker["image"], dev["image"])
        declared = set(self.compose["volumes"])
        self.assertEqual(
            declared,
            {
                "agent-history-workspace",
                "agent-history-claude-home",
                "agent-history-codex-home",
                "agent-history-github-auth",
                "agent-history-data",
            },
        )


if __name__ == "__main__":
    unittest.main()
