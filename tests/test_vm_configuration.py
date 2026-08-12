import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class VmConfigurationTests(unittest.TestCase):
    def git_check_ignored(self, path):
        return subprocess.run(
            ["git", "check-ignore", "--no-index", "-q", path],
            cwd=ROOT,
            check=False,
        ).returncode == 0

    def test_only_codex_hooks_file_is_not_ignored(self):
        for path in (
            ".codex/auth.json",
            ".codex/config.toml",
            ".codex/sessions/example.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.git_check_ignored(path))

        self.assertFalse(self.git_check_ignored(".codex/hooks.json"))

    def test_backup_directory_is_not_ignored_by_git(self):
        self.assertTrue(self.git_check_ignored("data/backups/example.db"))

    def test_vm_compose_uses_bind_mounts_without_host_path_creation(self):
        compose = (ROOT / "compose.vm.yaml").read_text()
        self.assertEqual(compose.count("create_host_path: false"), 8)
        self.assertNotIn("agent-history-data:/", compose)
        self.assertNotIn("agent-history-workspace:/", compose)

    def test_systemd_retries_detached_compose_start_after_mounts(self):
        unit = (ROOT / "systemd/agent-history-compose.service").read_text()
        self.assertIn("RequiresMountsFor=/srv/agent-history", unit)
        self.assertIn("Type=exec", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=15s", unit)
        self.assertIn("RemainAfterExit=yes", unit)
        self.assertIn("up -d --no-build", unit)
        self.assertNotIn("TimeoutStartSec=", unit)
        self.assertIn("failures are retried by systemd", unit)

    def test_vm_make_targets_and_purge_guard_exist(self):
        makefile = (ROOT / "Makefile").read_text()
        self.assertIn("vm-start:", makefile)
        self.assertIn("vm-worker-restart:", makefile)
        self.assertIn("AGENT_HISTORY_VM_MODE=1", makefile)
        self.assertIn("$(VM_RUN) start --no-build", makefile)
        self.assertIn("$(VM_RUN) worker-start --no-build", makefile)
        self.assertIn("vm-purge is intentionally disabled", makefile)

    def test_vm_init_checks_numeric_uid_and_gid(self):
        script = (ROOT / "scripts/agent-history-vm-init").read_text()
        self.assertIn("actual_uid", script)
        self.assertIn("actual_gid", script)
        self.assertIn("VM environment file does not exist", script)
        self.assertIn("not a readable regular file", script)
        self.assertIn("must define DEV_UID and DEV_GID", script)
        self.assertIn("must be numeric values", script)
        self.assertIn("UID/GID mismatch", script)
        self.assertNotIn('configured_uid="${configured_uid:-$actual_uid}"', script)
        self.assertNotIn('configured_gid="${configured_gid:-$actual_gid}"', script)
        self.assertIn('"$vm_root/workspace/projects"', script)
        self.assertIn('"$vm_root/project-state"', script)

    def test_vm_env_permissions_are_documented(self):
        docs = (ROOT / "docs/proxmox-vm.md").read_text()
        self.assertIn("0750 root:amida", docs)
        self.assertIn("0640 root:amida", docs)
        self.assertIn("docker info", docs)

    def test_vm_bootstrap_clones_before_vm_init(self):
        docs = (ROOT / "docs/proxmox-vm.md").read_text()
        self.assertLess(docs.index("git clone"), docs.index("agent-history-vm-init"))
        self.assertLess(docs.index("agent-history-vm-init"), docs.index("config --quiet"))

    def test_vm_clean_recovery_is_documented(self):
        docs = (ROOT / "docs/proxmox-vm.md").read_text()
        self.assertIn("通常運用では Compose を直接停止せず systemd 経由で操作する", docs)
        self.assertIn("sudo systemctl restart agent-history-compose.service", docs)


if __name__ == "__main__":
    unittest.main()
