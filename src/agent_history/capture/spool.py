"""Spool layout and the hook-side writer.

This module is imported by the Claude hook on Claude Code's critical path, so
it deliberately imports only `os` and `time` (both effectively free: `time` is
a builtin extension module). Every additional stdlib import costs measurable
milliseconds on hook startup -- `json` alone adds ~7.6ms and pulling in the
sanitizer adds ~24ms, which is most of the hook's 25ms budget. See
docs/design/spool-worker.md for the measurements.

Consequently the hook does not parse JSON, does not sanitize, and does not open
SQLite. It writes the raw hook payload to a spool file and exits; the worker
does all parsing, sanitizing, and database work off the critical path.
"""

import os
import time


SCHEMA_VERSION = 1

SPOOL_DIR_NAME = "spool"
DIR_TMP = "tmp"
DIR_PENDING = "pending"
DIR_FAILED = "failed"
LOCK_NAME = "worker.lock"
FILE_SUFFIX = ".spool"

DIR_MODE = 0o700
FILE_MODE = 0o600

# Hard cap on the hook input we retain. Claude Code payloads are far smaller;
# anything past this is a runaway tool result and is truncated rather than
# dropped, so the event itself survives.
MAX_INPUT_BYTES = 1 << 20

# The only spool guard we keep: without it a worker that is never started lets
# pending/ grow until the disk fills, which is a different class of failure
# than losing events. Checked on a sample of hook runs to keep the cost near
# zero (a full scandir on every hook would defeat the point).
MAX_PENDING_FILES = 50_000
PENDING_CHECK_SAMPLE = 128

READ_CHUNK_BYTES = 1 << 16


def default_db_path():
    """Resolve the database path without importing pathlib."""

    configured = os.environ.get("AGENT_HISTORY_DB")
    if configured:
        return os.path.expanduser(configured)
    here = os.path.abspath(__file__)
    # src/agent_history/capture/spool.py -> repository root
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    return os.path.join(root, "data", "agent_history.db")


def spool_root(db_path=None):
    configured = os.environ.get("AGENT_HISTORY_SPOOL_DIR")
    if configured:
        return os.path.expanduser(configured)
    path = db_path if db_path is not None else default_db_path()
    return os.path.join(os.path.dirname(os.path.abspath(str(path))), SPOOL_DIR_NAME)


def subdir(root, name):
    return os.path.join(root, name)


def ensure_dirs(root):
    """Create the spool tree with private permissions."""

    for name in (None, DIR_TMP, DIR_PENDING, DIR_FAILED):
        path = root if name is None else os.path.join(root, name)
        try:
            os.mkdir(path, DIR_MODE)
        except FileExistsError:
            pass
        except FileNotFoundError:
            os.makedirs(path, DIR_MODE, exist_ok=True)
    return root


def safe_label(value, limit=64):
    """Reduce a value to filename-safe characters without importing `re`."""

    if not value:
        return "unknown"
    out = []
    for character in str(value)[:limit]:
        if character.isalnum() or character in "_.-":
            out.append(character)
        else:
            out.append("_")
    return "".join(out) or "unknown"


def new_uid(pid=None):
    """Time-ordered unique id: lexical sort equals chronological sort."""

    if pid is None:
        pid = os.getpid()
    return "%019d-%d-%s" % (time.time_ns(), pid, os.urandom(4).hex())


def build_header(uid, event_name, size, pid=None, truncated=False):
    """Build the envelope header line without importing `json`.

    Every interpolated value is machine-generated and restricted to digits,
    hex, or `safe_label` output, so no JSON escaping is required.
    """

    if pid is None:
        pid = os.getpid()
    return '{"v":%d,"uid":"%s","ev":"%s","ts_ns":%d,"pid":%d,"size":%d,"truncated":%s}' % (
        SCHEMA_VERSION,
        uid,
        safe_label(event_name),
        int(uid.split("-", 1)[0]),
        pid,
        size,
        "true" if truncated else "false",
    )


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def read_input(stream, limit=MAX_INPUT_BYTES):
    """Read stdin up to `limit`, reporting the true size."""

    chunks = []
    kept = 0
    total = 0
    while True:
        chunk = stream.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if kept < limit:
            take = chunk[: limit - kept]
            chunks.append(take)
            kept += len(take)
    return b"".join(chunks), total


def pending_is_full(root, cap=MAX_PENDING_FILES):
    """Count pending entries with an early exit at `cap`."""

    pending = subdir(root, DIR_PENDING)
    count = 0
    try:
        with os.scandir(pending) as entries:
            for _ in entries:
                count += 1
                if count >= cap:
                    return True
    except FileNotFoundError:
        return False
    return False


def should_check_pending(sample=PENDING_CHECK_SAMPLE):
    return os.urandom(1)[0] < max(1, 256 // sample)


def write_event(root, event_name, data, total_size=None, uid=None):
    """Write one spool file and atomically publish it to pending/.

    No fsync: durability is deliberately traded for latency (see
    docs/design/spool-worker.md section 4). A crash can lose the last few
    seconds of events; it cannot produce a partial file, because readers only
    ever see the result of os.replace.
    """

    if uid is None:
        uid = new_uid()
    if total_size is None:
        total_size = len(data)
    label = safe_label(event_name)
    name = "%s-%s%s" % (uid, label, FILE_SUFFIX)
    tmp_path = os.path.join(root, DIR_TMP, name)
    pending_path = os.path.join(root, DIR_PENDING, name)
    header = build_header(uid, event_name, total_size, truncated=total_size > len(data))

    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        _write_all(fd, header.encode("utf-8"))
        _write_all(fd, b"\n")
        _write_all(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp_path, pending_path)
    return pending_path


def split_file(raw):
    """Split a spool file into its header line and raw payload bytes.

    Payloads may contain newlines, so everything after the first newline is
    payload.
    """

    header, separator, payload = raw.partition(b"\n")
    if not separator:
        return header, b""
    return header, payload
