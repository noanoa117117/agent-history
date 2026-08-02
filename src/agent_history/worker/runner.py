"""Single-writer worker loop.

Exactly one worker may write at a time. That is enforced with `flock`, which
the kernel releases automatically when the process dies, so a stale lock is not
possible and no PID-liveness heuristics are needed.

Crash safety is deliberately cheap: files are deleted only after the batch
commits. A crash in between leaves them in pending/, they are read again on the
next run, and the `UNIQUE(session_id, dedup_key)` index turns the re-insert
into a no-op. That is why this worker needs no processing/ directory, no
ownership handoff, and no orphan recovery.
"""

from __future__ import annotations

import fcntl
import os
import signal
import time
from typing import Optional

from ..capture import spool
from ..capture.claude_hook import hook_config
from ..db import PathLike, connect_db, get_db_path, transaction
from .ingest import (
    DEFAULT_BATCH_SIZE,
    BatchResult,
    claim_pending,
    ingest_batch,
    prepare_items,
)


DEFAULT_POLL_INTERVAL = 0.2
LOCK_BUSY_EXIT = 3
DB_MISSING_EXIT = 4


class WorkerBusyError(RuntimeError):
    """Another worker already holds the spool lock."""


class DatabaseMissingError(RuntimeError):
    """The database has not been initialized yet."""


def acquire_lock(root: str) -> int:
    """Take the exclusive spool lock, or raise WorkerBusyError."""

    spool.ensure_dirs(root)
    path = os.path.join(root, spool.LOCK_NAME)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, spool.FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise WorkerBusyError("another agent-history worker is already running") from exc
    os.ftruncate(fd, 0)
    os.write(fd, ("%d\n" % os.getpid()).encode("utf-8"))
    return fd


def release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def lock_holder_pid(root: str) -> Optional[int]:
    """Return the running worker's pid, or None when the lock is free."""

    path = os.path.join(root, spool.LOCK_NAME)
    if not os.path.exists(path):
        return None
    probe = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return int(handle.read().strip() or 0) or None
            except (OSError, ValueError):
                return -1  # held, pid unreadable
        fcntl.flock(probe, fcntl.LOCK_UN)
        return None
    finally:
        os.close(probe)


class Worker:
    def __init__(
        self,
        db_path: Optional[PathLike] = None,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.db_path = get_db_path(db_path)
        self.config = hook_config(self.db_path)
        self.root = spool.spool_root(self.db_path)
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self._stopping = False

    def request_stop(self, *_: object) -> None:
        self._stopping = True

    def install_signal_handlers(self) -> None:
        for received in (signal.SIGTERM, signal.SIGINT):
            signal.signal(received, self.request_stop)

    def process_batch(self, connection) -> BatchResult:
        """Ingest at most one batch. Returns an empty result when idle."""

        paths = claim_pending(self.root, self.batch_size)
        if not paths:
            return BatchResult()

        prepared, quarantined = prepare_items(self.root, paths, self.config)
        result = BatchResult(quarantined=quarantined)
        if not prepared:
            return result

        with transaction(connection, immediate=True):
            batch = ingest_batch(connection, prepared)
        result.inserted = batch.inserted
        result.duplicates = batch.duplicates

        # Only after the commit: a crash before this point simply replays the
        # batch, which the dedup index absorbs.
        for item, _ in prepared:
            try:
                os.unlink(item.path)
            except OSError:
                pass
        return result

    def drain(self, connection) -> BatchResult:
        """Process everything currently pending, then return."""

        totals = BatchResult()
        while True:
            result = self.process_batch(connection)
            if not result.handled:
                return totals
            totals.inserted += result.inserted
            totals.duplicates += result.duplicates
            totals.quarantined += result.quarantined

    def _check_database(self) -> None:
        if str(self.db_path) != ":memory:" and not os.path.exists(self.db_path):
            raise DatabaseMissingError(
                f"database is not initialized: {self.db_path} (run 'agent-history init')"
            )

    def run(self, *, drain_only: bool = False) -> BatchResult:
        """Hold the lock and process until stopped (or until drained)."""

        self._check_database()
        spool.ensure_dirs(self.root)
        lock = acquire_lock(self.root)
        totals = BatchResult()
        try:
            with connect_db(self.db_path, timeout=5.0) as connection:
                if drain_only:
                    return self.drain(connection)
                while not self._stopping:
                    result = self.process_batch(connection)
                    totals.inserted += result.inserted
                    totals.duplicates += result.duplicates
                    totals.quarantined += result.quarantined
                    if not result.handled:
                        time.sleep(self.poll_interval)
                # A stop request finishes the batch in flight rather than
                # abandoning it; anything still pending survives on disk.
                final = self.drain(connection)
                totals.inserted += final.inserted
                totals.duplicates += final.duplicates
                totals.quarantined += final.quarantined
        finally:
            release_lock(lock)
        return totals
