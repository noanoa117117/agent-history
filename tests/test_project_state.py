import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_history.commands import project
from agent_history.commands.session import create_session
from agent_history.db import connect_db, init_database


class ProjectStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        for command in (
            ["git", "init", "-q", str(self.repository)],
            ["git", "-C", str(self.repository), "config", "user.name", "Test User"],
            ["git", "-C", str(self.repository), "config", "user.email", "test@example.invalid"],
            ["git", "-C", str(self.repository), "commit", "--allow-empty", "-qm", "initial"],
        ):
            subprocess.run(command, check=True)
        self.db_path = self.root / "data" / "history.db"
        self.state_root = self.root / "project-state"
        init_database(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_register_update_and_link_uses_targets_without_new_schema(self):
        state = project.register_project(
            self.db_path,
            slug="example-project",
            name="Example Project",
            root_path=str(self.repository),
            state_root=str(self.state_root),
        )
        self.assertEqual(state["slug"], "example-project")
        self.assertTrue((self.state_root / "example-project" / "project.json").is_file())
        self.assertTrue((self.state_root / "example-project" / "progress.md").is_file())

        updated = project.update_project(
            self.db_path,
            slug="example-project",
            state_root=str(self.state_root),
            current_status="Implementing the VM runbook.",
            decision="Keep events as the source of truth.",
            next_action="Run the restore test.",
        )
        self.assertEqual(updated["current_status"], "Implementing the VM runbook.")
        progress = (self.state_root / "example-project" / "progress.md").read_text()
        self.assertIn("Keep events as the source of truth.", progress)
        self.assertIn("Run the restore test.", progress)

        session_id = create_session(self.db_path, source="manual", initial_cwd=str(self.repository))
        project.link_session(
            self.db_path,
            slug="example-project",
            session_id=session_id,
            state_root=str(self.state_root),
        )
        with connect_db(self.db_path) as connection:
            target = connection.execute(
                "SELECT target_type, slug, locator FROM targets WHERE id = ?", (state["target_id"],)
            ).fetchone()
            linked = connection.execute(
                "SELECT relation_type, assigned_by FROM session_targets WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(tuple(target), ("repository", "example-project", str(self.repository.resolve())))
        self.assertEqual(tuple(linked), ("worked_on", "project-state"))

    def test_rejects_non_repository_and_invalid_slug(self):
        with self.assertRaisesRegex(Exception, "slug"):
            project.register_project(
                self.db_path,
                slug="Invalid Slug",
                name="Invalid",
                root_path=str(self.repository),
                state_root=str(self.state_root),
            )
        with self.assertRaisesRegex(Exception, "does not exist"):
            project.register_project(
                self.db_path,
                slug="missing",
                name="Missing",
                root_path=str(self.root / "missing"),
                state_root=str(self.state_root),
            )

    def test_resolve_project_path_requires_registered_git_root_below_projects(self):
        projects = self.root / "projects"
        projects.mkdir()
        repository = projects / "example-project"
        self.repository.rename(repository)
        project.register_project(
            self.db_path,
            slug="example-project",
            name="Example Project",
            root_path=str(repository),
            state_root=str(self.state_root),
        )
        previous = os.environ.get("AGENT_HISTORY_PROJECTS_DIR")
        os.environ["AGENT_HISTORY_PROJECTS_DIR"] = str(projects)
        try:
            self.assertEqual(
                project.resolve_project_path(self.db_path, slug="example-project"),
                str(repository.resolve()),
            )
            with self.assertRaisesRegex(Exception, "not registered"):
                project.resolve_project_path(self.db_path, slug="missing")
            with self.assertRaisesRegex(Exception, "slug"):
                project.resolve_project_path(self.db_path, slug="../escape")
        finally:
            if previous is None:
                os.environ.pop("AGENT_HISTORY_PROJECTS_DIR", None)
            else:
                os.environ["AGENT_HISTORY_PROJECTS_DIR"] = previous
