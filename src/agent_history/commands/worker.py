"""Worker and spool operations."""

from __future__ import annotations

import os
import signal
import time
from typing import List, Optional

from ..capture import spool
from ..db import PathLike, get_db_path
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
