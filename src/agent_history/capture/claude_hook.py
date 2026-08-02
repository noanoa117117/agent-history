"""Non-blocking Claude Code command-hook adapter.

The adapter deliberately emits no stdout and returns success even when the
local recorder fails. Claude Code uses hook exit code 2 as a blocking signal,
so an observation-only hook must never return it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, List, Mapping, Optional, Tuple

from ..db import PathLike, connect_db, get_db_path, transaction
from ..sanitizer import normalize_json, sanitize_text
from ..timeutil import utc_now_iso


SUPPORTED_EVENTS: Tuple[str, ...] = (
    "SessionStart",
    "SessionEnd",
    "Setup",
    "InstructionsLoaded",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "PermissionDenied",
    "Notification",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "Stop",
    "StopFailure",
    "TeammateIdle",
    "ConfigChange",
    "CwdChanged",
    "FileChanged",
    "DirectoryAdded",
    "WorktreeCreate",
    "WorktreeRemove",
    "PreCompact",
    "PostCompact",
    "Elicitation",
    "ElicitationResult",
    "MessageDisplay",
)

EVENT_MAPPING: Mapping[str, Tuple[str, str]] = {
    "SessionStart": ("session_start", "system"),
    "SessionEnd": ("session_end", "system"),
    "Setup": ("setup", "system"),
    "InstructionsLoaded": ("instructions_loaded", "system"),
    "UserPromptSubmit": ("user_prompt", "human"),
    "UserPromptExpansion": ("user_prompt_expansion", "human"),
    "PreToolUse": ("tool_call", "claude"),
    "PermissionRequest": ("permission_request", "system"),
    "PostToolUse": ("tool_result", "tool"),
    "PostToolUseFailure": ("tool_error", "tool"),
    "PostToolBatch": ("tool_batch_result", "tool"),
    "PermissionDenied": ("permission_denied", "system"),
    "Notification": ("notification", "system"),
    "SubagentStart": ("subagent_start", "claude"),
    "SubagentStop": ("subagent_stop", "claude"),
    "TaskCreated": ("task_created", "system"),
    "TaskCompleted": ("task_completed", "system"),
    "Stop": ("assistant_stop", "claude"),
    "StopFailure": ("assistant_error", "system"),
    "TeammateIdle": ("teammate_idle", "system"),
    "ConfigChange": ("config_change", "system"),
    "CwdChanged": ("cwd_changed", "system"),
    "FileChanged": ("file_changed", "tool"),
    "DirectoryAdded": ("directory_added", "tool"),
    "WorktreeCreate": ("worktree_create", "tool"),
    "WorktreeRemove": ("worktree_remove", "tool"),
    "PreCompact": ("compact_start", "system"),
    "PostCompact": ("compact_end", "system"),
    "Elicitation": ("elicitation", "system"),
    "ElicitationResult": ("elicitation_result", "human"),
    "MessageDisplay": ("assistant_message_delta", "claude"),
}

# Events that accept a `matcher` in settings.json, mirroring the matcher-aware
# event set in the Claude Code 2.1.220 binary. `"*"` is match-all for all of
# them (Claude treats "", "*", and ".*" alike). StopFailure is deliberately
# absent: it takes no matcher, so emitting one would be inaccurate.
MATCHER_EVENTS = {
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "PermissionDenied",
    "UserPromptExpansion",
    "SessionStart",
    "SessionEnd",
    "Setup",
    "PreCompact",
    "PostCompact",
    "Notification",
    "SubagentStart",
    "SubagentStop",
    "Elicitation",
    "ElicitationResult",
    "ConfigChange",
    "InstructionsLoaded",
    "DirectoryAdded",
}

# WorktreeCreate is a special Claude hook: a successful command hook must
# print the path of the worktree it created. A passive recorder cannot satisfy
# that contract without replacing Claude Code's worktree creation behavior.
# The adapter can still record a manually supplied WorktreeCreate payload, but
# the installer deliberately does not register it.
INSTALLABLE_EVENTS = tuple(
    event for event in SUPPORTED_EVENTS if event != "WorktreeCreate"
)

DEFAULT_MAX_CONTENT_BYTES = 65_536
DEFAULT_MAX_JSON_BYTES = 262_144
READ_CHUNK_BYTES = 65_536
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


@dataclass(frozen=True)
class HookConfig:
    db_path: Path
    max_content_bytes: int
    max_json_bytes: int
    max_input_bytes: int
    dead_letter_dir: Path
    log_path: Path


@dataclass(frozen=True)
class HookResult:
    ok: bool
    event_id: Optional[int] = None
    duplicate: bool = False
    error: Optional[str] = None
    dead_letter_path: Optional[Path] = None


def _positive_env(name: str, default: int, *, minimum: int = 256) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value >= minimum else default


def hook_config(db_path: Optional[PathLike] = None) -> HookConfig:
    path = get_db_path(db_path)
    data_dir = path.parent
    max_json = _positive_env("AGENT_HISTORY_HOOK_MAX_JSON_BYTES", DEFAULT_MAX_JSON_BYTES)
    return HookConfig(
        db_path=path,
        max_content_bytes=_positive_env(
            "AGENT_HISTORY_HOOK_MAX_CONTENT_BYTES", DEFAULT_MAX_CONTENT_BYTES
        ),
        max_json_bytes=max_json,
        max_input_bytes=max(max_json * 16, 1_048_576),
        dead_letter_dir=Path(
            os.environ.get("AGENT_HISTORY_HOOK_DEAD_LETTER_DIR", data_dir / "dead-letter")
        ).expanduser(),
        log_path=Path(
            os.environ.get("AGENT_HISTORY_HOOK_LOG", data_dir / "logs" / "claude-hooks.log")
        ).expanduser(),
    )


def _read_bytes(stream: BinaryIO, limit: int) -> Tuple[bytes, int, str, bool]:
    kept = bytearray()
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = stream.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        digest.update(chunk)
        total += len(chunk)
        if len(kept) < limit:
            kept.extend(chunk[: limit - len(kept)])
    return bytes(kept), total, digest.hexdigest(), total > limit


def _input_stream(stream: Optional[Any]) -> BinaryIO:
    if stream is not None:
        return stream.buffer if hasattr(stream, "buffer") else stream
    return sys.stdin.buffer


def _safe_mkdir(path: Path) -> None:
    absolute = Path(os.path.abspath(str(path)))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise OSError(f"refusing symlink path component: {current}")
        except FileNotFoundError:
            continue
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_ISLNK(path.lstat().st_mode):
        raise OSError(f"refusing symlink directory: {path}")
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _append_log(path: Path, message: str) -> None:
    try:
        _safe_mkdir(path.parent)
        if path.exists() and stat.S_ISLNK(path.lstat().st_mode):
            return
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, (message.rstrip() + "\n").encode("utf-8", errors="replace"))
        finally:
            os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        return


def _write_atomic_private(path: Path, text: str) -> None:
    _safe_mkdir(path.parent)
    if path.exists() and stat.S_ISLNK(path.lstat().st_mode):
        raise OSError(f"refusing symlink file: {path}")
    fd, temporary = tempfile.mkstemp(prefix=".agent-history-", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _error_log(config: HookConfig, event_name: Optional[str], message: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    safe_event = _safe_label(event_name or "unknown")
    _append_log(config.log_path, f"{now} event={safe_event} error={message[:500]}")


def _safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:64] or "unknown"


def _dead_letter(
    config: HookConfig,
    *,
    event_name: Optional[str],
    reason: str,
    input_size: int,
    input_sha256: str,
    payload: Optional[Mapping[str, Any]],
) -> Optional[Path]:
    safe_payload: Dict[str, Any] = {
        "dead_letter_reason": reason[:500],
        "hook_event": event_name,
        "input_size": input_size,
        "input_sha256": input_sha256,
    }
    if payload is not None:
        try:
            # Normalize home-relative paths first, so a dead letter does not
            # keep the real transcript path that a stored event would hide.
            safe_payload["payload"] = json.loads(
                normalize_json(
                    json.dumps(
                        _normalize_paths(dict(payload)),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    label="dead-letter payload",
                ).text
            )
        except Exception:
            safe_payload["payload"] = {"omitted": True}
    try:
        text = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > config.max_json_bytes:
            text = json.dumps(
                {
                    "dead_letter_reason": reason[:500],
                    "hook_event": event_name,
                    "input_size": input_size,
                    "input_sha256": input_sha256,
                    "payload_omitted": True,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        filename = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            + "-"
            + _safe_label((event_name or "unknown").lower())
            + "-"
            + uuid.uuid4().hex[:12]
            + ".json"
        )
        destination = config.dead_letter_dir / filename
        _write_atomic_private(destination, text + "\n")
        return destination
    except Exception as exc:
        _error_log(config, event_name, f"dead-letter write failed: {type(exc).__name__}")
        return None


def _normalize_home(value: str) -> str:
    home = str(Path.home())
    if value == home:
        return "~"
    if value.startswith(home + os.sep):
        return "~" + value[len(home) :]
    return value


def _normalize_paths(value: Any, key: Optional[str] = None) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize_paths(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_paths(item, key) for item in value]
    if isinstance(value, str) and key and (key.endswith("_path") or key in {"file_path", "path"}):
        return _normalize_home(value)
    return value


def _truncate_text(text: str, maximum: int) -> Tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text, False
    marker = "\n...[truncated]...\n"
    marker_bytes = len(marker.encode("utf-8"))
    if maximum <= marker_bytes:
        return encoded[:maximum].decode("utf-8", errors="ignore"), True
    head_bytes = (maximum - marker_bytes) // 2
    tail_bytes = maximum - marker_bytes - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore")
    return head + marker + tail, True


def _looks_like_binary(text: str) -> bool:
    """Detect an encoded blob, not merely punctuation-free prose.

    Requiring no whitespace and a mixed alphabet keeps ordinary command output
    and long prose out: a length divisible by four and a base64 alphabet alone
    matched plain sentences, which were then discarded as binary.
    """

    if len(text) < 1024 or len(text) % 4 != 0:
        return False
    if not BASE64_RE.fullmatch(text) or any(character.isspace() for character in text):
        return False
    body = text.rstrip("=")
    has_lower = any(character.islower() for character in body)
    has_upper = any(character.isupper() for character in body)
    has_digit = any(character.isdigit() for character in body)
    # Encoded bytes mix cases or include digits; a single-case alphabetic run
    # of this length is far more likely to be text. Detection is best effort:
    # an unrecognized blob is truncated rather than lost.
    return (has_lower and has_upper) or has_digit


def _limit_value(value: Any, maximum: int) -> Tuple[Any, bool]:
    if isinstance(value, str):
        if _looks_like_binary(value):
            return "<REDACTED_BINARY>", True
        return _truncate_text(value, maximum)
    if isinstance(value, list):
        items = []
        changed = False
        for item in value:
            limited, item_changed = _limit_value(item, maximum)
            items.append(limited)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            limited, item_changed = _limit_value(item, maximum)
            result[str(key)] = limited
            changed = changed or item_changed
        return result, changed
    return value, False


def _compact_value(value: Any, maximum: int) -> Any:
    if isinstance(value, str):
        return _truncate_text(value, maximum)[0]
    if isinstance(value, list):
        return [_compact_value(item, maximum) for item in value[:128]]
    if isinstance(value, dict):
        return {key: _compact_value(item, maximum) for key, item in list(value.items())[:128]}
    return value


def _serialize_payload(
    payload: Mapping[str, Any],
    *,
    event_name: str,
    original_size: int,
    truncated: bool,
    maximum: int,
) -> Tuple[str, bool]:
    candidate: Dict[str, Any] = dict(payload)
    candidate["hook_event"] = event_name
    candidate["original_size"] = original_size
    candidate["truncated"] = bool(truncated)
    candidate["stored_size"] = 0

    for _ in range(5):
        text = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        actual_size = len(text.encode("utf-8"))
        if actual_size <= maximum:
            if candidate["stored_size"] != actual_size:
                candidate["stored_size"] = actual_size
                continue
            return text, bool(truncated)
        candidate = _compact_value(candidate, max(64, maximum // 16))
        candidate["hook_event"] = event_name
        candidate["original_size"] = original_size
        candidate["truncated"] = True
        candidate["stored_size"] = 0

    minimal = {
        "hook_event": event_name,
        "claude_session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        "tool_name": payload.get("tool_name"),
        "original_size": original_size,
        "stored_size": 0,
        "truncated": True,
        "payload_omitted": True,
    }
    for _ in range(3):
        text = json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
        actual_size = len(text.encode("utf-8"))
        if minimal["stored_size"] == actual_size:
            break
        minimal["stored_size"] = actual_size
    if len(text.encode("utf-8")) <= maximum:
        return text, True
    # The configured minimum is deliberately large enough for this marker,
    # but keep the fallback valid JSON even if a caller supplies a smaller cap.
    fallback = {
        "hook_event": event_name,
        "original_size": original_size,
        "truncated": True,
        "payload_omitted": True,
    }
    return json.dumps(fallback, ensure_ascii=False, separators=(",", ":")), True


def _prepare_payload(
    payload: Mapping[str, Any], event_name: str, config: HookConfig, input_size: int
) -> Tuple[Dict[str, Any], str, bool, bool]:
    normalized = _normalize_paths(dict(payload))
    sanitized_json = normalize_json(
        json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
        label="Claude hook payload",
    )
    sanitized_payload = json.loads(sanitized_json.text)
    original_size = input_size
    limited_payload, limit_changed = _limit_value(
        sanitized_payload, config.max_content_bytes
    )
    if not isinstance(limited_payload, dict):
        limited_payload = {"payload": limited_payload}
        limit_changed = True
    if "session_id" in limited_payload:
        limited_payload.setdefault("claude_session_id", limited_payload["session_id"])
    if "tool_response" in limited_payload and "tool_output" not in limited_payload:
        limited_payload["tool_output"] = limited_payload["tool_response"]
    text, json_truncated = _serialize_payload(
        limited_payload,
        event_name=event_name,
        original_size=original_size,
        truncated=limit_changed,
        maximum=config.max_json_bytes,
    )
    return json.loads(text), text, sanitized_json.changed, (limit_changed or json_truncated)


def _value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)


def _tool_summary(tool_name: str, tool_input: Any) -> str:
    data = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash" and data.get("command") is not None:
        return str(data["command"])
    if tool_name in {"Read", "Write", "Edit", "MultiEdit"} and data.get("file_path"):
        return str(data["file_path"])
    if tool_name in {"Glob", "Grep"}:
        parts = [str(data[key]) for key in ("pattern", "path", "glob") if data.get(key)]
        if parts:
            return " ".join(parts)
    if tool_name == "WebFetch" and data.get("url"):
        return str(data["url"])
    if tool_name == "WebSearch" and data.get("query"):
        return str(data["query"])
    if tool_name in {"Agent", "Task"}:
        return str(data.get("description") or data.get("prompt") or data.get("task") or "")
    return _value_text(tool_input)


def _content(event_name: str, payload: Mapping[str, Any], maximum: int) -> Tuple[str, bool]:
    tool_name = str(payload.get("tool_name") or "tool")
    if event_name == "UserPromptSubmit":
        text = _value_text(payload.get("prompt"))
    elif event_name == "UserPromptExpansion":
        text = "Expanded prompt: " + _value_text(payload.get("prompt") or payload.get("command_name"))
    elif event_name == "PreToolUse":
        text = f"{tool_name}: {_tool_summary(tool_name, payload.get('tool_input'))}"
    elif event_name in {"PostToolUse", "PostToolUseFailure"}:
        suffix = payload.get("tool_response") or payload.get("error") or payload.get("reason")
        text = f"{tool_name} {'failed' if event_name.endswith('Failure') else 'completed'}"
        if suffix not in (None, ""):
            text += f": {_value_text(suffix)}"
    elif event_name == "PostToolBatch":
        calls = payload.get("tool_calls")
        text = f"PostToolBatch: {len(calls) if isinstance(calls, list) else 0} tool calls"
    elif event_name == "Notification":
        text = "Claude Code notification: " + _value_text(payload.get("message"))
    elif event_name == "Stop":
        text = _value_text(payload.get("last_assistant_message")) or "Claude response completed"
    elif event_name == "StopFailure":
        text = "Claude response failed: " + _value_text(payload.get("error") or payload.get("last_assistant_message"))
    elif event_name == "SessionStart":
        text = f"Claude session started ({_value_text(payload.get('source')) or 'unknown'})"
    elif event_name == "SessionEnd":
        text = f"Claude session ended ({_value_text(payload.get('reason')) or 'unknown'})"
    elif event_name in {"SubagentStart", "SubagentStop"}:
        text = f"{event_name}: {_value_text(payload.get('agent_type') or payload.get('agent_id'))}"
        if event_name == "SubagentStop" and payload.get("last_assistant_message"):
            text += f": {_value_text(payload['last_assistant_message'])}"
    elif event_name in {"PreCompact", "PostCompact"}:
        text = f"{event_name}: {_value_text(payload.get('trigger'))}"
        if payload.get("compact_summary"):
            text += f" {_value_text(payload['compact_summary'])}"
    elif event_name == "CwdChanged":
        text = f"cwd changed: {_value_text(payload.get('old_cwd'))} -> {_value_text(payload.get('new_cwd'))}"
    elif event_name in {"WorktreeCreate", "WorktreeRemove"}:
        text = f"{event_name}: {_value_text(payload.get('name') or payload.get('worktree_path'))}"
    elif event_name == "InstructionsLoaded":
        text = f"Instructions loaded: {_value_text(payload.get('file_path'))}"
    elif event_name == "FileChanged":
        text = f"File changed: {_value_text(payload.get('file_path'))} ({_value_text(payload.get('event'))})"
    elif event_name == "ConfigChange":
        text = f"Config changed: {_value_text(payload.get('source') or payload.get('file_path'))}"
    elif event_name in {"Elicitation", "ElicitationResult"}:
        text = f"{event_name}: {_value_text(payload.get('message') or payload.get('action') or payload.get('mcp_server_name'))}"
    else:
        text = f"{event_name}: " + _value_text(
            payload.get("message")
            or payload.get("task_subject")
            or payload.get("teammate_name")
            or payload.get("delta")
            or payload.get("agent_type")
            or payload.get("trigger")
            or payload
        )
    return _truncate_text(text, maximum)


# Per-event identifier fields, taken from the Claude Code hook input schemas.
#
# Only fields that identify *this* event may appear here. In particular
# `prompt_id` must never be used: the schema defines it as a turn correlator
# carried by every event until the next user prompt, so treating it as an event
# id collapses every FileChanged, Notification, SubagentStart, TaskCreated, ...
# of a turn into a single row. `agent_id` is likewise turn-wide for hooks that
# fire inside a subagent, so it identifies an event only for SubagentStart and
# SubagentStop, where it names the subagent the event is about.
EVENT_IDENTITY_FIELDS: Mapping[str, Tuple[str, ...]] = {
    "PreToolUse": ("tool_use_id",),
    "PostToolUse": ("tool_use_id",),
    "PostToolUseFailure": ("tool_use_id",),
    "PermissionDenied": ("tool_use_id",),
    "SubagentStart": ("agent_id",),
    "SubagentStop": ("agent_id",),
    "TaskCreated": ("task_id",),
    "TaskCompleted": ("task_id",),
    "Elicitation": ("elicitation_id",),
    "ElicitationResult": ("elicitation_id",),
}


def _stable_source_id(event_name: str, payload: Mapping[str, Any]) -> Optional[str]:
    if event_name == "MessageDisplay" and payload.get("message_id") is not None:
        index = payload.get("index", payload.get("content_block_index", ""))
        return f"{event_name}:{payload['message_id']}:{index}"
    for key in EVENT_IDENTITY_FIELDS.get(event_name, ()):
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value):
            return f"{event_name}:{value}"
    return None


def _dedup_key(event_name: str, payload: Mapping[str, Any], sanitized_payload: Mapping[str, Any]) -> str:
    source_id = _stable_source_id(event_name, payload)
    identity: Dict[str, Any] = {"hook_event": event_name, "source_event_id": source_id}
    if source_id is None:
        # No per-event id: fall back to the full sanitized payload so that
        # events differing in any observable field stay distinct, and only a
        # byte-identical redelivery (the same hook registered in two settings
        # scopes) collapses.
        identity["prompt_id"] = payload.get("prompt_id")
        identity["timestamp"] = payload.get("timestamp") or payload.get("occurred_at")
        identity["payload"] = sanitized_payload
    else:
        identity["payload_identity"] = {
            "session_id": payload.get("session_id"),
            "tool_name": payload.get("tool_name"),
            "source_event_id": source_id,
        }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_event(
    config: HookConfig,
    *,
    event_name: str,
    payload: Mapping[str, Any],
    content: str,
    content_json: str,
    sensitivity: str,
    truncated: bool,
    source_event_id: Optional[str],
    dedup_key: str,
    occurred_at: str,
) -> Tuple[int, bool]:
    session_id = str(payload["session_id"])
    event_type, actor = EVENT_MAPPING[event_name]
    if str(config.db_path) != ":memory:":
        if config.db_path.is_symlink():
            raise RuntimeError("refusing to write through a symlink database path")
        if not config.db_path.exists():
            raise RuntimeError("database is not initialized")
    for attempt in range(3):
        try:
            with connect_db(config.db_path, timeout=0.25) as connection:
                with transaction(connection, immediate=True):
                    session = connection.execute(
                        """
                        SELECT id, model, initial_cwd, title, ended_at
                        FROM sessions
                        WHERE source = 'claude-code' AND source_session_id = ?
                        ORDER BY created_at
                        LIMIT 1
                        """,
                        (session_id,),
                    ).fetchone()
                    if session is None:
                        now = utc_now_iso()
                        connection.execute(
                            """
                            INSERT INTO sessions (
                                id, source, source_session_id, model, started_at,
                                initial_cwd, host_name, title, capture_status,
                                metadata_json, created_at
                            ) VALUES (?, 'claude-code', ?, ?, ?, ?, ?, ?, 'capturing', ?, ?)
                            """,
                            (
                                str(uuid.uuid4()),
                                session_id,
                                payload.get("model"),
                                occurred_at,
                                payload.get("cwd"),
                                socket.gethostname(),
                                payload.get("session_title"),
                                json.dumps(
                                    {"agent_type": payload.get("agent_type")}
                                    if payload.get("agent_type")
                                    else {},
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                now,
                            ),
                        )
                        session = connection.execute(
                            """
                            SELECT id, model, initial_cwd, title, ended_at
                            FROM sessions WHERE source = 'claude-code'
                              AND source_session_id = ? ORDER BY created_at LIMIT 1
                            """,
                            (session_id,),
                        ).fetchone()
                    db_session_id = str(session["id"])
                    connection.execute(
                        """
                        UPDATE sessions
                        SET model = COALESCE(model, ?),
                            initial_cwd = COALESCE(initial_cwd, ?),
                            title = COALESCE(title, ?)
                        WHERE id = ?
                        """,
                        (
                            payload.get("model"),
                            payload.get("cwd"),
                            payload.get("session_title"),
                            db_session_id,
                        ),
                    )
                    existing = connection.execute(
                        "SELECT id FROM events WHERE session_id = ? AND dedup_key = ?",
                        (db_session_id, dedup_key),
                    ).fetchone()
                    if existing is not None:
                        event_id = int(existing["id"])
                        duplicate = True
                    else:
                        sequence = connection.execute(
                            "SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_sequence "
                            "FROM events WHERE session_id = ?",
                            (db_session_id,),
                        ).fetchone()["next_sequence"]
                        cursor = connection.execute(
                            """
                            INSERT INTO events (
                                session_id, sequence_no, event_type, actor, content,
                                content_json, source_event_id, payload_size, truncated,
                                dedup_key, cwd, exit_code, occurred_at, sensitivity, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                db_session_id,
                                int(sequence),
                                event_type,
                                actor,
                                content,
                                content_json,
                                source_event_id,
                                len(content_json.encode("utf-8")),
                                int(truncated),
                                dedup_key,
                                payload.get("cwd"),
                                payload.get("exit_code"),
                                occurred_at,
                                sensitivity,
                                utc_now_iso(),
                            ),
                        )
                        event_id = int(cursor.lastrowid)
                        duplicate = False
                    if event_name == "SessionEnd":
                        connection.execute(
                            "UPDATE sessions SET ended_at = ?, capture_status = 'completed' "
                            "WHERE id = ?",
                            (occurred_at, db_session_id),
                        )
                    return event_id, duplicate
        except Exception as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if attempt < 2:
                time.sleep(0.05 * (2**attempt))
            else:
                raise
    raise RuntimeError("unreachable")


def process_hook(
    event_name: Optional[str] = None,
    *,
    input_bytes: Optional[bytes] = None,
    stdin: Optional[Any] = None,
    db_path: Optional[PathLike] = None,
) -> HookResult:
    config = hook_config(db_path)
    if input_bytes is None:
        retained, input_size, input_sha256, input_truncated = _read_bytes(
            _input_stream(stdin), config.max_input_bytes
        )
    else:
        input_size = len(input_bytes)
        input_sha256 = hashlib.sha256(input_bytes).hexdigest()
        input_truncated = input_size > config.max_input_bytes
        retained = input_bytes[: config.max_input_bytes]

    if input_truncated:
        reason = f"hook input exceeded hard limit {config.max_input_bytes} bytes"
        path = _dead_letter(
            config,
            event_name=event_name,
            reason=reason,
            input_size=input_size,
            input_sha256=input_sha256,
            payload=None,
        )
        _error_log(config, event_name, reason)
        return HookResult(False, error=reason, dead_letter_path=path)

    try:
        decoded = retained.decode("utf-8")
        parsed = json.loads(
            decoded,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
        if not isinstance(parsed, dict):
            raise ValueError("hook input must be a JSON object")
        payload: Dict[str, Any] = parsed
    except Exception as exc:
        reason = f"invalid hook JSON: {type(exc).__name__}"
        path = _dead_letter(
            config,
            event_name=event_name,
            reason=reason,
            input_size=input_size,
            input_sha256=input_sha256,
            payload=None,
        )
        _error_log(config, event_name, reason)
        return HookResult(False, error=reason, dead_letter_path=path)

    payload_event = payload.get("hook_event_name")
    if event_name and payload_event and event_name != payload_event:
        reason = "hook event argument does not match hook_event_name"
        path = _dead_letter(
            config,
            event_name=event_name,
            reason=reason,
            input_size=input_size,
            input_sha256=input_sha256,
            payload=payload,
        )
        _error_log(config, event_name, reason)
        return HookResult(False, error=reason, dead_letter_path=path)
    actual_event = event_name or (str(payload_event) if payload_event else None)
    if actual_event not in EVENT_MAPPING:
        reason = f"unsupported or missing hook event: {actual_event or 'unknown'}"
        path = _dead_letter(
            config,
            event_name=actual_event,
            reason=reason,
            input_size=input_size,
            input_sha256=input_sha256,
            payload=payload,
        )
        _error_log(config, actual_event, reason)
        return HookResult(False, error=reason, dead_letter_path=path)

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        reason = "hook input is missing session_id"
        path = _dead_letter(
            config,
            event_name=actual_event,
            reason=reason,
            input_size=input_size,
            input_sha256=input_sha256,
            payload=payload,
        )
        _error_log(config, actual_event, reason)
        return HookResult(False, error=reason, dead_letter_path=path)

    try:
        limited_payload, content_json, sanitized_changed, truncated = _prepare_payload(
            payload, actual_event, config, input_size
        )
        content, content_truncated = _content(actual_event, limited_payload, config.max_content_bytes)
        truncated = truncated or content_truncated
        if content_truncated:
            # Keep the JSON flag truthful if the human-readable projection was
            # shorter than the structured payload.
            limited_payload["truncated"] = True
            content_json, _ = _serialize_payload(
                limited_payload,
                event_name=actual_event,
                original_size=input_size,
                truncated=True,
                maximum=config.max_json_bytes,
            )
        dedup_key = _dedup_key(actual_event, payload, limited_payload)
        source_event_id = _stable_source_id(actual_event, payload)
        occurred_at = utc_now_iso()
        sensitivity = "sanitized" if sanitized_changed else "clean"
        event_id, duplicate = _record_event(
            config,
            event_name=actual_event,
            payload=limited_payload,
            content=content,
            content_json=content_json,
            sensitivity=sensitivity,
            truncated=truncated,
            source_event_id=source_event_id,
            dedup_key=dedup_key,
            occurred_at=occurred_at,
        )
        return HookResult(True, event_id=event_id, duplicate=duplicate)
    except Exception as exc:
        reason = f"hook recording failed: {type(exc).__name__}"
        safe_payload: Optional[Mapping[str, Any]] = None
        try:
            safe_payload = json.loads(
                normalize_json(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    label="dead-letter payload",
                ).text
            )
        except Exception:
            pass
        path = _dead_letter(
            config,
            event_name=actual_event,
            reason=reason,
            input_size=input_size,
            input_sha256=input_sha256,
            payload=safe_payload,
        )
        _error_log(config, actual_event, reason)
        return HookResult(False, error=reason, dead_letter_path=path)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = list(argv) if argv is not None else sys.argv[1:]
    db_path: Optional[str] = None
    event_name: Optional[str] = None
    index = 0
    while index < len(args):
        if args[index] == "--db" and index + 1 < len(args):
            db_path = args[index + 1]
            index += 2
            continue
        if args[index].startswith("--"):
            index += 1
            continue
        if event_name is None:
            event_name = args[index]
        index += 1
    try:
        process_hook(event_name, db_path=db_path)
    except Exception as exc:
        # Never allow an adapter-level I/O error to block Claude Code. There
        # may be no usable DB or dead-letter directory, so diagnostics are
        # best-effort and intentionally contain no input payload.
        try:
            config = hook_config(db_path)
            _error_log(config, event_name, f"uncaught hook error: {type(exc).__name__}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
