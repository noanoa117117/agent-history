import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from agent_history.capture import hook_fast, spool


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "bin" / "agent-history-claude-hook"

SAMPLE = {
    "session_id": "spool-test",
    "cwd": "/tmp",
    "hook_event_name": "SessionStart",
    "source": "startup",
}


def _run_hook(spool_dir, event_name, payload, db_path=None):
    """Run the real hook binary the way Claude Code runs it."""

    env = dict(os.environ)
    env["AGENT_HISTORY_SPOOL_DIR"] = str(spool_dir)
    if db_path is not None:
        env["AGENT_HISTORY_DB"] = str(db_path)
    return subprocess.run(
        [str(HOOK), event_name],
        input=payload if isinstance(payload, bytes) else json.dumps(payload).encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _spawn(args):
    spool_dir, index = args
    _run_hook(spool_dir, "SessionStart", dict(SAMPLE, prompt_id=f"p{index}"))
    return index


class SpoolWriterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.temp_dir.name, "spool")
        os.environ["AGENT_HISTORY_SPOOL_DIR"] = self.root
        spool.ensure_dirs(self.root)

    def tearDown(self):
        os.environ.pop("AGENT_HISTORY_SPOOL_DIR", None)
        self.temp_dir.cleanup()

    def pending(self):
        return sorted(os.listdir(spool.subdir(self.root, spool.DIR_PENDING)))

    def test_hook_never_opens_sqlite(self):
        """Acceptance criterion: the hook must not touch SQLite at all."""

        import sqlite3

        original = sqlite3.connect

        def explode(*args, **kwargs):
            raise AssertionError("the hook opened SQLite on the critical path")

        sqlite3.connect = explode
        try:
            hook_fast.run(["SessionStart"], stdin=io.BytesIO(json.dumps(SAMPLE).encode()))
        finally:
            sqlite3.connect = original
        self.assertEqual(len(self.pending()), 1)

    def test_hook_never_fsyncs(self):
        """Acceptance criterion: no fsync on Claude Code's critical path."""

        original = os.fsync

        def explode(*args, **kwargs):
            raise AssertionError("the hook called fsync on the critical path")

        os.fsync = explode
        try:
            hook_fast.run(["Stop"], stdin=io.BytesIO(json.dumps(SAMPLE).encode()))
        finally:
            os.fsync = original
        self.assertEqual(len(self.pending()), 1)

    def test_hook_does_not_load_expensive_modules(self):
        """The import budget is the whole reason the hook is a separate module.

        Measured on this project's reference machine: `json` costs ~7.6ms and
        the sanitizer (which pulls `re`) ~24ms, against a 25ms hook budget.
        Checks what actually ends up in sys.modules, not what the source says.
        """

        probe = (
            "import sys, os, io\n"
            f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
            f"os.environ['AGENT_HISTORY_SPOOL_DIR'] = {self.root!r}\n"
            "from agent_history.capture import hook_fast\n"
            "hook_fast.run(['Stop'], stdin=io.BytesIO(b'{\"session_id\":\"x\"}'))\n"
            "heavy = ('json', 're', 'sqlite3', 'pathlib', 'dataclasses', "
            "'tempfile', 'typing', 'hashlib', 'socket', 'uuid')\n"
            "sys.stderr.write(','.join(m for m in heavy if m in sys.modules))\n"
        )
        result = subprocess.run(
            [sys.executable, "-S", "-c", probe],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        loaded = [name for name in result.stderr.decode().split(",") if name]
        self.assertEqual(loaded, [], f"hook loaded expensive modules: {loaded}")

    def test_spool_file_is_header_plus_raw_payload(self):
        payload = json.dumps(SAMPLE).encode()
        path = hook_fast.run(["SessionStart"], stdin=io.BytesIO(payload))
        raw = Path(path).read_bytes()
        header_bytes, body = spool.split_file(raw)
        header = json.loads(header_bytes)
        self.assertEqual(header["v"], spool.SCHEMA_VERSION)
        self.assertEqual(header["ev"], "SessionStart")
        self.assertEqual(header["size"], len(payload))
        self.assertFalse(header["truncated"])
        self.assertEqual(body, payload)

    def test_payload_with_newlines_survives(self):
        payload = b'{"session_id":"x",\n"hook_event_name":"Stop",\n"a":"b"}'
        path = hook_fast.run(["Stop"], stdin=io.BytesIO(payload))
        _, body = spool.split_file(Path(path).read_bytes())
        self.assertEqual(body, payload)

    def test_permissions_are_private(self):
        path = hook_fast.run(["Stop"], stdin=io.BytesIO(json.dumps(SAMPLE).encode()))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), spool.FILE_MODE)
        for name in (spool.DIR_TMP, spool.DIR_PENDING, spool.DIR_FAILED):
            directory = spool.subdir(self.root, name)
            self.assertEqual(stat.S_IMODE(os.stat(directory).st_mode), spool.DIR_MODE)

    def test_no_tmp_leftovers_and_names_sort_chronologically(self):
        for index in range(10):
            hook_fast.run(["Stop"], stdin=io.BytesIO(json.dumps(SAMPLE).encode()))
            time.sleep(0.001)
        names = self.pending()
        self.assertEqual(len(names), 10)
        self.assertEqual(names, sorted(names))
        self.assertEqual(os.listdir(spool.subdir(self.root, spool.DIR_TMP)), [])
        stamps = [int(name.split("-", 1)[0]) for name in names]
        self.assertEqual(stamps, sorted(stamps))

    def test_oversized_input_is_truncated_not_dropped(self):
        big = b'{"session_id":"x","hook_event_name":"Stop","blob":"' + b"A" * (2 << 20) + b'"}'
        path = hook_fast.run(["Stop"], stdin=io.BytesIO(big))
        header_bytes, body = spool.split_file(Path(path).read_bytes())
        header = json.loads(header_bytes)
        self.assertTrue(header["truncated"])
        self.assertEqual(header["size"], len(big))
        self.assertLessEqual(len(body), spool.MAX_INPUT_BYTES)

    def test_hook_exits_zero_when_spool_is_unwritable(self):
        os.chmod(spool.subdir(self.root, spool.DIR_TMP), 0o500)
        try:
            result = _run_hook(self.root, "Stop", SAMPLE)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
        finally:
            os.chmod(spool.subdir(self.root, spool.DIR_TMP), spool.DIR_MODE)

    def test_hook_writes_nothing_to_stdout(self):
        result = _run_hook(self.root, "SessionStart", SAMPLE)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_concurrent_hooks_do_not_collide(self):
        count = 32
        with ProcessPoolExecutor(max_workers=8) as pool:
            list(pool.map(_spawn, [(self.root, index) for index in range(count)]))
        names = self.pending()
        self.assertEqual(len(names), count)
        self.assertEqual(len(set(names)), count)
        self.assertEqual(os.listdir(spool.subdir(self.root, spool.DIR_TMP)), [])

    def test_pending_cap_stops_the_hook(self):
        pending_dir = spool.subdir(self.root, spool.DIR_PENDING)
        for index in range(5):
            Path(pending_dir, f"filler-{index}{spool.FILE_SUFFIX}").write_bytes(b"{}")
        self.assertTrue(spool.pending_is_full(self.root, cap=5))
        self.assertFalse(spool.pending_is_full(self.root, cap=50))


class HookLatencyTests(unittest.TestCase):
    """Guards the acceptance criterion. Process spawn alone costs ~12ms here,
    so the budget is 25ms rather than the originally hoped-for 10ms."""

    BUDGET_MS = float(os.environ.get("AGENT_HISTORY_HOOK_BUDGET_MS", "25"))
    SAMPLES = int(os.environ.get("AGENT_HISTORY_HOOK_SAMPLES", "40"))

    @unittest.skipIf(
        os.environ.get("AGENT_HISTORY_SKIP_PERF"), "performance test disabled"
    )
    def test_hook_p95_within_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = os.path.join(temp_dir, "spool")
            payload = json.dumps(SAMPLE).encode()
            latencies = []
            for _ in range(self.SAMPLES):
                started = time.perf_counter()
                _run_hook(root, "SessionStart", payload)
                latencies.append((time.perf_counter() - started) * 1000)
            latencies.sort()
            p50 = latencies[len(latencies) // 2]
            p95 = latencies[int(len(latencies) * 0.95)]
            self.assertLess(
                p95,
                self.BUDGET_MS,
                f"hook p95={p95:.1f}ms p50={p50:.1f}ms exceeds {self.BUDGET_MS}ms budget",
            )


if __name__ == "__main__":
    unittest.main()
