"""Event presets for Claude Code hook installation.

Registering every event was what made the first Stage 2 rollout unusable: the
high-frequency events fire hundreds to thousands of times per turn, and each
one cost ~170ms of synchronous SQLite commit. The spool worker removes the
per-event cost, but frequency still drives spool volume, so the default stays
conservative and the noisy events are opt-in.
"""

from __future__ import annotations

from typing import Mapping, Tuple

from .capture.claude_hook import INSTALLABLE_EVENTS, SUPPORTED_EVENTS


#: Events that fire many times per turn. Excluded from every preset but `full`.
HIGH_FREQUENCY_EVENTS: Tuple[str, ...] = (
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "FileChanged",
    "MessageDisplay",
)

#: The session skeleton: a handful of events per turn.
MINIMAL_EVENTS: Tuple[str, ...] = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Stop",
)

DEFAULT_PRESET = "balanced"


def _ordered(names) -> Tuple[str, ...]:
    """Keep preset output in the canonical SUPPORTED_EVENTS order."""

    wanted = set(names)
    return tuple(event for event in SUPPORTED_EVENTS if event in wanted)


#: Everything installable except the noisy events, so the default stays useful
#: without flooding the spool.
BALANCED_EVENTS: Tuple[str, ...] = _ordered(
    event for event in INSTALLABLE_EVENTS if event not in HIGH_FREQUENCY_EVENTS
)

FULL_EVENTS: Tuple[str, ...] = _ordered(INSTALLABLE_EVENTS)

PRESETS: Mapping[str, Tuple[str, ...]] = {
    "minimal": _ordered(MINIMAL_EVENTS),
    "balanced": BALANCED_EVENTS,
    "full": FULL_EVENTS,
}

PRESET_DESCRIPTIONS: Mapping[str, str] = {
    "minimal": "session skeleton only (a few events per turn)",
    "balanced": "default: everything except the high-frequency events",
    "full": "every installable event, including high-frequency ones (opt-in)",
}


def preset_events(name: str) -> Tuple[str, ...]:
    try:
        return PRESETS[name]
    except KeyError:
        raise ValueError(
            f"unknown preset: {name!r} (choose from {', '.join(sorted(PRESETS))})"
        ) from None


def is_rotational_disk() -> bool:
    """Best-effort detection of a spinning disk, used to warn about `full`."""

    try:
        import glob

        for path in glob.glob("/sys/block/*/queue/rotational"):
            with open(path, "r", encoding="utf-8") as handle:
                if handle.read().strip() == "1":
                    return True
    except OSError:
        pass
    return False
