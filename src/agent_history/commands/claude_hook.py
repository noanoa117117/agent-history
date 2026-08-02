"""Claude Code hook installation, diagnostics, and session listing."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..capture.claude_hook import (
    INSTALLABLE_EVENTS,
    MATCHER_EVENTS,
    hook_config,
)
from ..db import PathLike, connect_db, get_db_path, REPOSITORY_ROOT
from ..presets import (
    DEFAULT_PRESET,
    HIGH_FREQUENCY_EVENTS,
    PRESET_DESCRIPTIONS,
    is_rotational_disk,
    preset_events,
)
from . import CommandError
from .session import list_sessions, render_session_list


HOOK_MARKER = "agent-history-claude-hook"


def adapter_path() -> Path:
    return REPOSITORY_ROOT / "bin" / "agent-history-claude-hook"


def settings_path(scope: str = "user", *, cwd: Optional[PathLike] = None) -> Path:
    if scope == "user":
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(config_dir).expanduser() / "settings.json" if config_dir else Path.home() / ".claude" / "settings.json"
    base = Path(cwd or Path.cwd()).resolve()
    if scope == "project":
        return base / ".claude" / "settings.json"
    if scope == "local":
        return base / ".claude" / "settings.local.json"
    raise CommandError(f"unsupported scope: {scope}")


def _hook_command(event_name: Optional[str] = None) -> str:
    """Build the registered command.

    The event name is baked into each command because the hook cannot afford to
    import `json` to read `hook_event_name` out of the payload; argv is free.
    The worker still cross-checks the two.
    """

    adapter = str(adapter_path())
    return f"{adapter} {event_name}" if event_name else adapter


def _new_hook_group(event_name: str) -> Dict[str, Any]:
    group: Dict[str, Any] = {
        "hooks": [{"type": "command", "command": _hook_command(event_name)}]
    }
    if event_name in MATCHER_EVENTS:
        group["matcher"] = "*"
    return group


def _is_agent_handler(handler: Any) -> bool:
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and isinstance(handler.get("command"), str)
        and HOOK_MARKER in handler["command"]
    )


def _load_settings(path: Path) -> Tuple[Dict[str, Any], bool]:
    if path.is_symlink():
        raise CommandError(f"refusing to modify symlink settings file: {path}")
    if not path.exists():
        return {}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"could not read valid settings JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CommandError("Claude settings JSON must be an object")
    return data, True


def _copy_without_agent_hooks(data: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    result = json.loads(json.dumps(data, ensure_ascii=False))
    hooks = result.get("hooks")
    if hooks is None:
        return result, 0
    if not isinstance(hooks, dict):
        raise CommandError("Claude settings hooks must be an object")
    removed = 0
    for event_name in list(hooks):
        groups = hooks[event_name]
        if not isinstance(groups, list):
            raise CommandError(f"Claude settings hooks.{event_name} must be an array")
        new_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                new_groups.append(group)
                continue
            handlers = group["hooks"]
            kept = [handler for handler in handlers if not _is_agent_handler(handler)]
            removed += len(handlers) - len(kept)
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                new_groups.append(new_group)
        if new_groups:
            hooks[event_name] = new_groups
        else:
            del hooks[event_name]
    if not hooks:
        result.pop("hooks", None)
    return result, removed


def _merge_agent_hooks(
    data: Dict[str, Any], events: Iterable[str] = INSTALLABLE_EVENTS
) -> Tuple[Dict[str, Any], int, List[str]]:
    result = json.loads(json.dumps(data, ensure_ascii=False))
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise CommandError("Claude settings hooks must be an object")
    added = 0
    already = []
    for event_name in events:
        groups = hooks.setdefault(event_name, [])
        if not isinstance(groups, list):
            raise CommandError(f"Claude settings hooks.{event_name} must be an array")
        if any(
            isinstance(group, dict)
            and isinstance(group.get("hooks"), list)
            and any(_is_agent_handler(handler) for handler in group["hooks"])
            for group in groups
        ):
            already.append(event_name)
            continue
        groups.append(_new_hook_group(event_name))
        added += 1
    return result, added, already


def _backup(path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = path.with_name(path.name + ".bak-" + timestamp)
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    return backup


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise CommandError(f"refusing to modify symlink settings file: {path}")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    json.loads(text)
    fd, temporary = tempfile.mkstemp(prefix=".agent-history-settings-", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def install(
    *,
    apply: bool = False,
    scope: str = "user",
    path: Optional[PathLike] = None,
    preset: str = DEFAULT_PRESET,
) -> str:
    try:
        events = preset_events(preset)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    target = Path(path).expanduser() if path else settings_path(scope)
    existing, existed = _load_settings(target)
    merged, added, already = _merge_agent_hooks(existing, events)
    lines = [
        "Claude Code hook installation (dry-run)" if not apply else "Claude Code hook installation",
        f"Settings: {target}",
        f"Adapter: {_hook_command()}",
        f"Scope: {scope}",
        f"Preset: {preset} ({PRESET_DESCRIPTIONS[preset]})",
        f"Existing agent-history events: {len(already)}",
        f"Events to add: {added}",
        "Installed events: " + ", ".join(events),
        "Not installed by passive adapter: WorktreeCreate (hook must create and print a worktree path)",
    ]
    if preset == "full":
        lines.append(
            "Warning: 'full' registers high-frequency events ("
            + ", ".join(HIGH_FREQUENCY_EVENTS)
            + ")."
        )
        if is_rotational_disk():
            lines.append(
                "Warning: a rotational disk was detected. 'full' is not recommended here; "
                "prefer --preset balanced."
            )
    if not apply:
        lines.append("No files changed. Use --apply to write the merged settings.")
        return "\n".join(lines)
    backup = None
    if existed and added:
        backup = _backup(target)
    if added:
        _atomic_write_json(target, merged)
    lines[0] = "Claude Code hook installation applied"
    lines.append(f"Backup: {backup if backup else 'not needed'}")
    lines.append("Changed: yes" if added else "Changed: no (already installed)")
    return "\n".join(lines)


def uninstall(
    *,
    apply: bool = False,
    scope: str = "user",
    path: Optional[PathLike] = None,
) -> str:
    target = Path(path).expanduser() if path else settings_path(scope)
    existing, existed = _load_settings(target)
    cleaned, removed = _copy_without_agent_hooks(existing)
    lines = [
        "Claude Code hook uninstallation (dry-run)" if not apply else "Claude Code hook uninstallation",
        f"Settings: {target}",
        f"Agent-history handlers to remove: {removed}",
    ]
    if not apply:
        lines.append("No files changed. Use --apply to write the result.")
        return "\n".join(lines)
    backup = None
    if existed and removed:
        backup = _backup(target)
        _atomic_write_json(target, cleaned)
    lines[0] = "Claude Code hook uninstallation applied"
    lines.append(f"Backup: {backup if backup else 'not needed'}")
    lines.append("Changed: yes" if removed else "Changed: no (nothing installed)")
    return "\n".join(lines)


def _claude_version() -> str:
    try:
        result = subprocess.run(
            ["claude", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return (result.stdout or result.stderr).strip() or "unavailable"
    except Exception:
        return "unavailable"


def _settings_hook_count(path: Path) -> Tuple[str, int]:
    if not path.exists():
        return "missing", 0
    try:
        data, _ = _load_settings(path)
    except CommandError:
        return "invalid", 0
    hooks = data.get("hooks", {})
    count = 0
    if isinstance(hooks, dict):
        for groups in hooks.values():
            if isinstance(groups, list):
                for group in groups:
                    if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                        count += sum(_is_agent_handler(handler) for handler in group["hooks"])
    return "installed" if count else "not-installed", count


def status(*, db_path: Optional[PathLike] = None, scope: str = "user") -> str:
    path = settings_path(scope)
    state, count = _settings_hook_count(path)
    resolved_db = get_db_path(db_path)
    initialized = False
    last_session = None
    if resolved_db.exists():
        try:
            with connect_db(resolved_db) as connection:
                initialized = (
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sessions'"
                    ).fetchone()
                    is not None
                )
                if initialized:
                    row = connection.execute(
                        """
                        SELECT id, source_session_id, title, started_at
                        FROM sessions WHERE source='claude-code'
                        ORDER BY started_at DESC LIMIT 1
                        """
                    ).fetchone()
                    last_session = dict(row) if row else None
        except Exception:
            initialized = False
    capture_config = hook_config(resolved_db)
    dead_letter_dir = capture_config.dead_letter_dir
    dead_letters = len(list(dead_letter_dir.glob("*.json"))) if dead_letter_dir.exists() else 0
    log_path = capture_config.log_path
    recent_errors: List[str] = []
    if log_path.exists():
        try:
            recent_errors = log_path.read_text(encoding="utf-8").splitlines()[-5:]
        except OSError:
            recent_errors = ["unreadable"]
    lines = [
        f"Claude Code version: {_claude_version()}",
        f"Settings: {path}",
        f"Hook installation: {state} ({count} handlers)",
        "Installed events: " + ", ".join(INSTALLABLE_EVENTS),
        "Adapter-supported but not installed: WorktreeCreate (requires path-producing hook behavior)",
        f"Database: {resolved_db}",
        f"Database initialized: {'yes' if initialized else 'no'}",
        f"Dead-letter files: {dead_letters}",
        "Recent hook errors:",
    ]
    lines.extend(f"- {line}" for line in recent_errors) if recent_errors else lines.append("- none")
    if last_session:
        lines.append(
            "Last Claude session: "
            f"{last_session['id']} source_session_id={last_session['source_session_id']} "
            f"title={last_session['title'] or '-'} started={last_session['started_at']}"
        )
    else:
        lines.append("Last Claude session: none")
    return "\n".join(lines)


def claude_sessions(db_path: Optional[PathLike] = None, *, limit: int = 20) -> str:
    rows = list_sessions(db_path, source="claude-code", limit=limit)
    return render_session_list(rows)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-history claude-hook")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--apply", action="store_true")
    install_parser.add_argument("--scope", choices=("user", "project", "local"), default="user")
    install_parser.add_argument("--settings-path")
    install_parser.add_argument(
        "--preset", choices=sorted(PRESET_DESCRIPTIONS), default=DEFAULT_PRESET
    )
    uninstall_parser = subparsers.add_parser("uninstall")
    uninstall_parser.add_argument("--apply", action="store_true")
    uninstall_parser.add_argument("--scope", choices=("user", "project", "local"), default="user")
    uninstall_parser.add_argument("--settings-path")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--db")
    status_parser.add_argument("--scope", choices=("user", "project", "local"), default="user")
    sessions_parser = subparsers.add_parser("sessions")
    sessions_parser.add_argument("--db")
    sessions_parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "install":
        print(
            install(
                apply=args.apply,
                scope=args.scope,
                path=args.settings_path,
                preset=args.preset,
            )
        )
    elif args.command == "uninstall":
        print(uninstall(apply=args.apply, scope=args.scope, path=args.settings_path))
    elif args.command == "status":
        print(status(db_path=args.db, scope=args.scope))
    elif args.command == "sessions":
        print(claude_sessions(args.db, limit=args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
