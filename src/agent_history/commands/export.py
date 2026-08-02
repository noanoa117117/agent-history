"""Markdown export of search results."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..db import PathLike
from .search import search_events


def _markdown_value(value: Any) -> str:
    return "-" if value is None or value == "" else str(value)


def export_markdown(
    db_path: Optional[PathLike],
    *,
    query: str,
    source: Optional[str] = None,
    target: Optional[str] = None,
    from_value: Optional[str] = None,
    to_value: Optional[str] = None,
    session_id: Optional[str] = None,
    context_before: int = 0,
    context_after: int = 0,
    output: Optional[str] = None,
) -> str:
    results = search_events(
        db_path,
        query,
        source=source,
        target=target,
        from_value=from_value,
        to_value=to_value,
        session_id=session_id,
        context_before=context_before,
        context_after=context_after,
        limit=1000,
    )
    sessions: Dict[str, List[Dict[str, Any]]] = {}
    for result in results:
        sessions.setdefault(result["session_id"], []).append(result)

    lines = [
        "# Agent History Export",
        "",
        "## Export metadata",
        "",
        f"- Query: {_markdown_value(query)}",
        f"- Generated at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- Source filters: {_markdown_value(source)}",
        f"- Target filters: {_markdown_value(target)}",
        f"- Date range: {_markdown_value(from_value)} - {_markdown_value(to_value)}",
        f"- Session count: {len(sessions)}",
        f"- Event count: {len(results)}",
        "",
    ]

    for session_id_key, session_events in sessions.items():
        first = session_events[0]
        title = first.get("title") or session_id_key
        lines.extend(
            [
                f"## Session: {title}",
                "",
                f"- Session ID: {session_id_key}",
                f"- Source: {_markdown_value(first.get('source'))}",
                f"- Model: {_markdown_value(first.get('model'))}",
                f"- Started: {_markdown_value(first.get('started_at'))}",
                f"- Working directory: {_markdown_value(first.get('initial_cwd'))}",
                f"- Targets: {_markdown_value(', '.join(first.get('targets', [])))}",
                "",
            ]
        )
        for event in session_events:
            lines.extend(
                [
                    f"### Event {event['sequence_no']}",
                    "",
                    f"- Event ID: {event['event_id']}",
                    f"- Sequence: {event['sequence_no']}",
                    f"- Time: {event['occurred_at']}",
                    f"- Type: {event['event_type']}",
                    f"- Actor: {event['actor']}",
                    f"- Match: {'yes' if event['matched'] else 'context'}",
                    "",
                    event["content"] if event["content"] is not None else "(structured JSON only)",
                    "",
                ]
            )

    markdown = "\n".join(lines).rstrip() + "\n"
    if output:
        Path(output).write_text(markdown, encoding="utf-8")
    return markdown
