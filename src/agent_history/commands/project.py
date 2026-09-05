"""Filesystem-backed project state built on the existing repository targets.

The events database remains the source of observable history.  This module
stores only the concise current state needed to resume work and registers each
repository as an existing ``targets`` row; it deliberately adds no new SQLite
schema or writer service.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..db import PathLike, connect_db, get_db_path
from ..timeutil import utc_now_iso
from . import CommandError
from . import target as target_commands


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
STATE_FILE_NAME = "project.json"
PROGRESS_FILE_NAME = "progress.md"


def project_state_root(db_path: Optional[PathLike] = None) -> Path:
    configured = os.environ.get("AGENT_HISTORY_PROJECT_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    # The normal VM DB is /srv/agent-history/data/agent_history.db.
    return get_db_path(db_path).expanduser().resolve().parent.parent / "project-state"


def _validate_slug(slug: str) -> None:
    if not SLUG_RE.fullmatch(slug):
        raise CommandError("slug must use lowercase letters, digits, and hyphens (1-63 chars)")


def _run_git(root_path: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root_path), *arguments],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise CommandError("git is required to register a project") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "not a Git worktree"
        raise CommandError(f"invalid project root {root_path}: {message}") from exc
    return result.stdout.strip()


def _optional_git(root_path: Path, *arguments: str) -> Optional[str]:
    """Return no value for normal Git queries that have no configured value."""

    try:
        return _run_git(root_path, *arguments) or None
    except CommandError:
        return None


def _git_details(root_path: Path) -> Dict[str, Optional[str]]:
    resolved = Path(_run_git(root_path, "rev-parse", "--show-toplevel")).resolve()
    if resolved != root_path.resolve():
        raise CommandError(
            f"root path must be the Git repository root: {root_path} (actual: {resolved})"
        )
    remote = _optional_git(root_path, "config", "--get", "remote.origin.url")
    branch = _run_git(root_path, "branch", "--show-current") or None
    default_branch = None
    try:
        symbolic = _run_git(root_path, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        default_branch = symbolic.rsplit("/", 1)[-1]
    except CommandError:
        default_branch = branch
    return {
        "root_path": str(resolved),
        "git_remote": remote,
        "current_branch": branch,
        "default_branch": default_branch,
        "commit_sha": _run_git(root_path, "rev-parse", "HEAD"),
    }


def _state_paths(state_root: Path, slug: str) -> tuple[Path, Path]:
    state_dir = state_root / slug
    return state_dir / STATE_FILE_NAME, state_dir / PROGRESS_FILE_NAME


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _load_state(state_root: Path, slug: str) -> Dict[str, Any]:
    _validate_slug(slug)
    state_path, _ = _state_paths(state_root, slug)
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CommandError(f"project state does not exist: {state_path}") from exc
    except json.JSONDecodeError as exc:
        raise CommandError(f"project state is invalid JSON: {state_path}") from exc
    if not isinstance(payload, dict) or payload.get("slug") != slug:
        raise CommandError(f"project state is invalid: {state_path}")
    return payload


def _bullet(items: List[str]) -> List[str]:
    return [f"- {item}" for item in items] if items else ["- None"]


def render_progress(state: Dict[str, Any]) -> str:
    """Render only resumable state; full work logs stay in SQLite events."""

    lines = [
        "# Project Progress",
        "",
        "## Goal",
        "",
        state.get("name", state["slug"]),
        "",
        "## Current Status",
        "",
        state.get("current_status") or "Not started",
        "",
        "## Completed",
        "",
        *_bullet(state.get("completed", [])),
        "",
        "## Decisions",
        "",
        *_bullet(state.get("decisions", [])),
        "",
        "## Blockers",
        "",
        *_bullet(state.get("blockers", [])),
        "",
        "## Next Actions",
        "",
        *_bullet(state.get("next_actions", [])),
        "",
        "## Verification",
        "",
        f"- Repository: `{state['root_path']}`",
        f"- Branch: `{state.get('current_branch') or 'detached'}`",
        f"- Commit: `{state.get('commit_sha') or 'unknown'}`",
        "",
        "## Last Updated",
        "",
        state["updated_at"],
        "",
    ]
    return "\n".join(lines)


def _save_state(state_root: Path, state: Dict[str, Any]) -> None:
    state["updated_at"] = utc_now_iso()
    state_path, progress_path = _state_paths(state_root, state["slug"])
    _write_private(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    _write_private(progress_path, render_progress(state))


def register_project(
    db_path: Optional[PathLike],
    *,
    slug: str,
    name: str,
    root_path: str,
    state_root: Optional[str] = None,
) -> Dict[str, Any]:
    _validate_slug(slug)
    if not name.strip():
        raise CommandError("name must not be empty")
    repository = Path(root_path).expanduser()
    if not repository.is_dir():
        raise CommandError(f"project root does not exist: {repository}")
    details = _git_details(repository)
    resolved_state_root = Path(state_root).expanduser() if state_root else project_state_root(db_path)

    metadata = json.dumps(
        {
            "root_path": details["root_path"],
            "git_remote": details["git_remote"],
            "default_branch": details["default_branch"],
            "current_branch": details["current_branch"],
            "commit_sha": details["commit_sha"],
            "project_state": str(resolved_state_root / slug),
        },
        ensure_ascii=False,
    )
    target_id = target_commands.add_target(
        db_path,
        target_type="repository",
        slug=slug,
        name=name.strip(),
        locator=details["root_path"],
        metadata_json=metadata,
        update=True,
    )
    state: Dict[str, Any] = {
        "version": 1,
        "slug": slug,
        "name": name.strip(),
        **details,
        "target_id": target_id,
        "current_status": "Registered; work has not started.",
        "completed": [],
        "decisions": [],
        "blockers": [],
        "next_actions": [],
    }
    _save_state(resolved_state_root, state)
    return state


def update_project(
    db_path: Optional[PathLike],
    *,
    slug: str,
    state_root: Optional[str] = None,
    current_status: Optional[str] = None,
    completed: Optional[str] = None,
    decision: Optional[str] = None,
    blocker: Optional[str] = None,
    next_action: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_state_root = Path(state_root).expanduser() if state_root else project_state_root(db_path)
    state = _load_state(resolved_state_root, slug)
    if current_status is not None:
        if not current_status.strip():
            raise CommandError("current status must not be empty")
        state["current_status"] = current_status.strip()
    for key, value in (
        ("completed", completed),
        ("decisions", decision),
        ("blockers", blocker),
        ("next_actions", next_action),
    ):
        if value is not None:
            if not value.strip():
                raise CommandError(f"{key} entry must not be empty")
            state.setdefault(key, []).append(value.strip())
    # Capture the latest Git identity without modifying the repository.
    state.update(_git_details(Path(state["root_path"])))
    _save_state(resolved_state_root, state)
    return state


def show_project(
    db_path: Optional[PathLike], *, slug: str, state_root: Optional[str] = None
) -> str:
    root = Path(state_root).expanduser() if state_root else project_state_root(db_path)
    state = _load_state(root, slug)
    _, progress_path = _state_paths(root, slug)
    return f"Project: {state['name']} ({slug})\nState: {progress_path}\n\n{render_progress(state)}"


def link_session(
    db_path: Optional[PathLike], *, slug: str, session_id: str, state_root: Optional[str] = None
) -> None:
    root = Path(state_root).expanduser() if state_root else project_state_root(db_path)
    state = _load_state(root, slug)
    target_commands.add_session_target(
        db_path,
        session_id=session_id,
        target_id=int(state["target_id"]),
        relation_type="worked_on",
        confidence=1.0,
        assigned_by="project-state",
    )


def resolve_project_path(db_path: Optional[PathLike], *, slug: str) -> str:
    """Resolve a registered repository slug to a safe container workspace path."""

    _validate_slug(slug)
    with connect_db(db_path) as connection:
        row = connection.execute(
            "SELECT locator FROM targets WHERE target_type = 'repository' AND slug = ?",
            (slug,),
        ).fetchone()
    if row is None:
        raise CommandError(f"project is not registered: {slug}")
    if not row["locator"]:
        raise CommandError(f"registered project has no root path: {slug}")

    projects_root = Path(
        os.environ.get("AGENT_HISTORY_PROJECTS_DIR", "/workspace/projects")
    ).resolve()
    repository = Path(str(row["locator"])).resolve()
    try:
        repository.relative_to(projects_root)
    except ValueError as exc:
        raise CommandError(
            f"registered project is outside projects directory: {repository}"
        ) from exc
    if repository == projects_root:
        raise CommandError("project root must be below the projects directory")
    if not repository.is_dir():
        raise CommandError(f"registered project does not exist: {repository}")
    actual = Path(_run_git(repository, "rev-parse", "--show-toplevel")).resolve()
    if actual != repository:
        raise CommandError(
            f"registered project root is not the Git repository root: {repository}"
        )
    return str(repository)
