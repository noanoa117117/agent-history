"""CLI command implementations."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence


class CommandError(Exception):
    """An expected user-facing command error."""


def display_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def format_table(rows: Iterable[Mapping[str, Any]], columns: Sequence[str]) -> str:
    materialized = [[display_value(row.get(column)) for column in columns] for row in rows]
    widths = [len(column) for column in columns]
    for row in materialized:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    header = "  ".join(column.ljust(widths[index]) for index, column in enumerate(columns))
    separator = "  ".join("-" * width for width in widths)
    lines = [header, separator]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in materialized
    )
    return "\n".join(lines)
