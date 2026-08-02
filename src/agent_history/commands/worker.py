"""Worker and spool operations."""

from __future__ import annotations

import os
import signal
import sqlite3
import sys
import time
from typing import List, Optional

from ..capture import spool
from ..db import PathLike, connect_db, get_db_path, init_database
from ..timeutil import utc_now_iso
from ..worker.runner import (
    DatabaseMissingError,
    Worker,
    WorkerBusyError,
    lock_holder_pid,
)
from . import CommandError


def _root(db_path: Optional[PathLike]) -> str:
    return spool.spool_root(get_db_path(db_path))


def _count(root: str, name: str, suffix: str = spool.FILE_SUFFIX) -> int:
    try:
        with os.scandir(spool.subdir(root, name)) as entries:
            return sum(1 for entry in entries if entry.is_file() and entry.name.endswith(suffix))
    except FileNotFoundError:
        return 0


def _bytes(root: str, name: str) -> int:
    total = 0
    try:
        with os.scandir(spool.subdir(root, name)) as entries:
            for entry in entries:
                if entry.is_file():
                    total += entry.stat().st_size
    except FileNotFoundError:
        return 0
    return total


REQUIRED_TABLES = ("sessions", "events", "events_fts")
DEFAULT_DB_WAIT_SECONDS = 60.0


def _log(message: str) -> None:
    """Line-buffered stderr logging so `docker logs` shows progress live."""

    sys.stderr.write(f"{utc_now_iso()} agent-history-worker: {message}\n")
    sys.stderr.flush()


def _schema_ready(db_path: PathLike) -> bool:
    if not os.path.exists(db_path):
        return False
    try:
        with connect_db(db_path, timeout=2.0) as connection:
            present = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
            if not set(REQUIRED_TABLES).issubset(present):
                return False
            # The worker's idempotent insert depends on this index existing.
            indexes = {
                row["name"]
                for row in connection.execute("PRAGMA index_list(events)")
            }
            return "idx_events_session_dedup_key" in indexes
    except sqlite3.Error:
        return False


def ensure_database_ready(
    db_path: PathLike,
    *,
    timeout: float = DEFAULT_DB_WAIT_SECONDS,
    log=_log,
) -> None:
    """Make the database usable, or give up with a reason.

    Prefers running the existing idempotent initializer over merely waiting,
    so a fresh data volume does not need a manual `agent-history init`. Only
    runs it when the schema is actually missing: `init` rebuilds the FTS index,
    which should not happen on every restart.

    Bounded on purpose -- a worker that retried forever would hide a broken
    volume behind an endless restart loop.
    """

    deadline = time.monotonic() + timeout
    last_error: Optional[str] = None
    attempted_init = False
    while True:
        try:
            if _schema_ready(db_path):
                return
            if not attempted_init:
                log(f"database is not initialized; applying schema to {db_path}")
            attempted_init = True
            init_database(db_path)
            if _schema_ready(db_path):
                log("database is ready")
                return
            last_error = "schema still incomplete after initialization"
        except Exception as exc:  # noqa: BLE001 - report any cause, then retry
            last_error = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            raise CommandError(
                f"database not ready after {timeout:g}s: {db_path}"
                + (f" ({last_error})" if last_error else "")
            )
        time.sleep(1.0)


def worker_run(
    *,
    db_path: Optional[PathLike] = None,
    batch_size: int = 50,
    db_wait_seconds: float = DEFAULT_DB_WAIT_SECONDS,
) -> str:
    """Run the worker in the foreground. This is the container entry point.

    Never daemonizes: under Compose the process must stay PID 1's child, or
    the container would exit immediately and restart forever. Logs to stderr
    as it goes rather than returning a summary at the end, and lets the
    Worker's SIGTERM handler finish the in-flight batch.
    """

    path = get_db_path(db_path)
    _log(f"starting: db={path} spool={spool.spool_root(path)} batch_size={batch_size}")
    ensure_database_ready(path, timeout=db_wait_seconds)

    worker = Worker(path, batch_size=batch_size, logger=_log)
    worker.install_signal_handlers()
    totals = worker.run(wait_for_lock=True)
    _log(
        "stopped: inserted=%d duplicates=%d quarantined=%d"
        % (totals.inserted, totals.duplicates, totals.quarantined)
    )
    # Progress already went to stderr; returning text would print a stray line.
    return ""


def worker_start(
    *,
    db_path: Optional[PathLike] = None,
    detach: bool = False,
    batch_size: int = 50,
) -> str:
    worker = Worker(db_path, batch_size=batch_size)
    if detach:
        # Double fork so the worker survives the shell that started it.
        if os.fork():
            time.sleep(0.3)
            pid = lock_holder_pid(worker.root)
            if pid is None:
                raise CommandError("worker failed to start (lock not held)")
            return f"Worker started (pid {pid})\nSpool: {worker.root}"
        os.setsid()
        if os.fork():
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for stream in (0, 1, 2):
            os.dup2(devnull, stream)
        try:
            worker.install_signal_handlers()
            worker.run()
        except Exception:
            pass
        os._exit(0)

    worker.install_signal_handlers()
    try:
        totals = worker.run()
    except WorkerBusyError as exc:
        raise CommandError(str(exc)) from exc
    except DatabaseMissingError as exc:
        raise CommandError(str(exc)) from exc
    return (
        f"Worker stopped. inserted={totals.inserted} "
        f"duplicates={totals.duplicates} quarantined={totals.quarantined}"
    )


def worker_stop(*, db_path: Optional[PathLike] = None, timeout: float = 10.0) -> str:
    root = _root(db_path)
    pid = lock_holder_pid(root)
    if pid is None:
        return "Worker is not running."
    if pid <= 0:
        raise CommandError("worker lock is held but its pid could not be read")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lock_holder_pid(root) is None:
            return f"Worker stopped (pid {pid})."
        time.sleep(0.1)
    return f"Sent SIGTERM to pid {pid}, but it is still running after {timeout:g}s."


def worker_drain(*, db_path: Optional[PathLike] = None, batch_size: int = 50) -> str:
    worker = Worker(db_path, batch_size=batch_size)
    try:
        totals = worker.run(drain_only=True)
    except WorkerBusyError as exc:
        raise CommandError(
            f"{exc}: stop it first, or let the running worker drain the spool"
        ) from exc
    except DatabaseMissingError as exc:
        raise CommandError(str(exc)) from exc
    return (
        f"Drained. inserted={totals.inserted} "
        f"duplicates={totals.duplicates} quarantined={totals.quarantined}"
    )


def worker_status(*, db_path: Optional[PathLike] = None) -> str:
    root = _root(db_path)
    pid = lock_holder_pid(root)
    if pid is None:
        state = "not running"
    elif pid > 0:
        state = f"running (pid {pid})"
    else:
        state = "running (pid unknown)"
    return "\n".join(
        [
            f"Worker: {state}",
            f"Spool: {root}",
            f"Database: {get_db_path(db_path)}",
            f"Pending: {_count(root, spool.DIR_PENDING)}",
            f"Failed: {_count(root, spool.DIR_FAILED)}",
        ]
    )


def spool_status(*, db_path: Optional[PathLike] = None) -> str:
    root = _root(db_path)
    pending = _count(root, spool.DIR_PENDING)
    return "\n".join(
        [
            f"Spool: {root}",
            f"Pending: {pending} files, {_bytes(root, spool.DIR_PENDING)} bytes",
            f"Failed: {_count(root, spool.DIR_FAILED)} files",
            f"Unpublished (tmp): {_count(root, spool.DIR_TMP, '')} files",
            f"Pending cap: {spool.MAX_PENDING_FILES}"
            + ("  [FULL - hook is dropping events]" if pending >= spool.MAX_PENDING_FILES else ""),
        ]
    )


def failed_list(*, db_path: Optional[PathLike] = None, limit: int = 20) -> str:
    root = _root(db_path)
    failed_dir = spool.subdir(root, spool.DIR_FAILED)
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(failed_dir)
            if entry.is_file() and entry.name.endswith(spool.FILE_SUFFIX)
        )
    except FileNotFoundError:
        names = []
    if not names:
        return "No failed events."
    lines = [f"Failed events: {len(names)} (showing up to {limit})"]
    for name in names[:limit]:
        reason = ""
        try:
            with open(os.path.join(failed_dir, name + ".reason"), "r", encoding="utf-8") as handle:
                reason = handle.read().strip()
        except OSError:
            reason = "(no reason recorded)"
        lines.append(f"  {name}\n    {reason}")
    return "\n".join(lines)


def failed_purge(*, db_path: Optional[PathLike] = None, older_than_days: float = 0.0) -> str:
    root = _root(db_path)
    failed_dir = spool.subdir(root, spool.DIR_FAILED)
    cutoff = time.time() - older_than_days * 86400 if older_than_days else None
    removed = 0
    try:
        entries: List[os.DirEntry] = list(os.scandir(failed_dir))
    except FileNotFoundError:
        return "No failed events."
    for entry in entries:
        if not entry.is_file():
            continue
        if cutoff is not None and entry.stat().st_mtime > cutoff:
            continue
        try:
            os.unlink(entry.path)
            if entry.name.endswith(spool.FILE_SUFFIX):
                removed += 1
        except OSError:
            pass
    scope = f" older than {older_than_days:g} days" if cutoff is not None else ""
    return f"Purged {removed} failed events{scope}."
