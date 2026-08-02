"""Lean Claude Code hook entry point.

Runs on Claude Code's critical path. It must stay cheap and must never block
Claude Code, so it only reads stdin and writes one spool file:

  * no JSON parsing (importing `json` costs ~7.6ms)
  * no sanitizing (importing the sanitizer costs ~24ms)
  * no SQLite, no fsync
  * always exits 0, even when spooling fails

The event name arrives as argv[1] because the installer bakes it into each
registered command; parsing it out of the payload would require `json`. The
worker cross-checks it against the payload's `hook_event_name`.
"""

import os
import sys

# Works both as `python3 -S path/to/hook_fast.py` and as `-m`. The bin wrapper
# uses the plain script path, which leaves the package off sys.path.
sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from agent_history.capture import spool  # noqa: E402


def _debug(message):
    if os.environ.get("AGENT_HISTORY_HOOK_DEBUG"):
        try:
            sys.stderr.write("agent-history hook: %s\n" % message)
        except Exception:
            pass


def run(argv, stdin=None):
    event_name = argv[0] if argv else "unknown"
    source = argv[1] if len(argv) > 1 else None
    stream = stdin if stdin is not None else sys.stdin.buffer
    data, total = spool.read_input(stream)
    if not data:
        return None

    root = spool.spool_root()
    spool.ensure_dirs(root)

    # Sampled so the scan cost is amortised to roughly nothing per hook.
    if spool.should_check_pending() and spool.pending_is_full(root):
        _debug("pending spool is full, dropping event")
        return None

    return spool.write_event(root, event_name, data, total_size=total, source=source)


def main(argv=None):
    args = list(argv) if argv is not None else sys.argv[1:]
    try:
        run(args)
    except Exception as exc:
        # An observation-only hook must never fail Claude Code. Exit code 2 is
        # Claude's blocking signal, so every path here returns 0.
        _debug("%s: %s" % (type(exc).__name__, exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
