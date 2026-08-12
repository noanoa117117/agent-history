import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_history.commands.event import add_event
from agent_history.commands.session import create_session
from agent_history.commands.search import search_events
from agent_history.db import init_database


ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "scripts" / "agent-history-backup"
RESTORE = ROOT / "scripts" / "agent-history-backup-restore"


class BackupRestoreTests(unittest.TestCase):
    def test_archive_uses_online_sqlite_backup_and_restores_verified_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "agent-history"
            data = root / "data"
            db_path = data / "agent_history.db"
            init_database(db_path)
            session_id = create_session(db_path, source="manual", initial_cwd="/workspace/projects/demo")
            add_event(
                db_path,
                session_id=session_id,
                event_type="note",
                actor="human",
                content="restoreverification searchable event",
            )
            pending = data / "spool" / "pending"
            failed = data / "spool" / "failed"
            pending.mkdir(parents=True)
            failed.mkdir(parents=True)
            (pending / "pending.spool").write_text("pending")
            (failed / "failed.spool").write_text("failed")
            state = root / "project-state" / "demo"
            state.mkdir(parents=True)
            (state / "progress.md").write_text("# Project Progress\n")

            environment = os.environ.copy()
            environment.update(
                {
                    "AGENT_HISTORY_VM_ROOT": str(root),
                    "AGENT_HISTORY_REPO_PATH": str(ROOT),
                    "AGENT_HISTORY_DATA_DIR": str(data),
                    "AGENT_HISTORY_DB": str(db_path),
                    "AGENT_HISTORY_PROJECT_STATE_DIR": str(root / "project-state"),
                    "AGENT_HISTORY_BACKUP_DIR": str(root / "backups"),
                    "AGENT_HISTORY_VM_ENV_FILE": str(root / "missing.env"),
                    "AGENT_HISTORY_BACKUP_KEEP": "2",
                }
            )
            backup = subprocess.run(
                [str(BACKUP)], env=environment, check=True, text=True, stdout=subprocess.PIPE
            ).stdout.strip()
            archive = Path(backup)
            self.assertTrue(archive.is_file())
            self.assertTrue(Path(backup + ".sha256").is_file())

            restored = Path(temporary) / "restored"
            subprocess.run([str(RESTORE), backup, str(restored)], check=True)
            self.assertTrue((restored / "data" / "spool" / "pending" / "pending.spool").is_file())
            self.assertTrue((restored / "data" / "spool" / "failed" / "failed.spool").is_file())
            self.assertTrue((restored / "project-state" / "demo" / "progress.md").is_file())
            results = search_events(restored / "data" / "agent_history.db", "restoreverification")
            self.assertEqual(len(results), 1)

    def test_restore_refuses_an_existing_or_live_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "empty.tar.gz"
            subprocess.run(["tar", "-czf", str(archive), "--files-from", "/dev/null"], check=True)
            existing = Path(temporary) / "existing"
            existing.mkdir()
            result = subprocess.run([str(RESTORE), str(archive), str(existing)], check=False)
            self.assertNotEqual(result.returncode, 0)
