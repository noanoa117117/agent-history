"""FTS5 search and context expansion."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..db import PathLike, connect_db
from ..timeutil import parse_filter_bound
from . import CommandError


def build_fts_query(query: str) -> str:
    """Quote user terms so FTS syntax cannot escape the intended search."""

    terms = re.findall(r"\S+", query.strip())
    if not terms:
        raise CommandError("search query must not be empty")
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _search_terms(query: str) -> List[str]:
    terms = re.findall(r"\S+", query.strip())
    if not terms:
        raise CommandError("search query must not be empty")
    return terms


def _like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _target_slugs(connection: Any, session_id: str) -> List[str]:
    rows = connection.execute(
        """
        SELECT t.slug
        FROM session_targets AS st
        JOIN targets AS t ON t.id = st.target_id
        WHERE st.session_id = ?
        ORDER BY t.slug
        """,
        (session_id,),
    ).fetchall()
    return [str(row["slug"]) for row in rows]


def _event_dict(connection: Any, row: Any, *, matched: bool) -> Dict[str, Any]:
    result = {
        "event_id": int(row["event_id"]),
        "session_id": row["session_id"],
        "sequence_no": int(row["sequence_no"]),
        "occurred_at": row["occurred_at"],
        "source": row["source"],
        "event_type": row["event_type"],
        "actor": row["actor"],
        "content": row["content"],
        "targets": _target_slugs(connection, row["session_id"]),
        "matched": matched,
        "title": row["title"],
        "model": row["model"],
        "started_at": row["started_at"],
        "initial_cwd": row["initial_cwd"],
        "cwd": row["cwd"],
        "exit_code": row["exit_code"],
        "sensitivity": row["sensitivity"],
    }
    return result


def _fetch_event(connection: Any, session_id: str, sequence_no: int) -> Optional[Any]:
    return connection.execute(
        """
        SELECT e.id AS event_id, e.session_id, e.sequence_no, e.event_type,
               e.actor, e.content, e.cwd, e.exit_code, e.occurred_at,
               e.sensitivity, s.source, s.title, s.model, s.started_at, s.initial_cwd
        FROM events AS e
        JOIN sessions AS s ON s.id = e.session_id
        WHERE e.session_id = ? AND e.sequence_no = ?
        """,
        (session_id, sequence_no),
    ).fetchone()


def search_events(
    db_path: Optional[PathLike],
    query: str,
    *,
    source: Optional[str] = None,
    session_id: Optional[str] = None,
    target: Optional[str] = None,
    from_value: Optional[str] = None,
    to_value: Optional[str] = None,
    event_type: Optional[str] = None,
    actor: Optional[str] = None,
    limit: int = 20,
    context_before: int = 0,
    context_after: int = 0,
) -> List[Dict[str, Any]]:
    if limit < 1:
        raise CommandError("limit must be at least 1")
    if context_before < 0 or context_after < 0:
        raise CommandError("context-before and context-after must not be negative")
    terms = _search_terms(query)
    fts_query = build_fts_query(query)

    like_conditions = " AND ".join("e2.content LIKE ? ESCAPE '\\'" for _ in terms)
    # FTS5 is the primary index. LIKE is a narrow fallback for text such as
    # Japanese without spaces, where unicode61 may keep a whole mixed-script
    # run as one token and cannot find an English substring inside it.
    where: List[str] = []
    parameters: List[Any] = [fts_query]
    parameters.extend(_like_pattern(term) for term in terms)
    if source:
        where.append("s.source = ?")
        parameters.append(source)
    if session_id:
        where.append("e.session_id = ?")
        parameters.append(session_id)
    if target:
        where.append(
            "EXISTS (SELECT 1 FROM session_targets AS st "
            "JOIN targets AS t ON t.id = st.target_id "
            "WHERE st.session_id = e.session_id AND t.slug = ?)"
        )
        parameters.append(target)
    if from_value:
        where.append("e.occurred_at >= ?")
        parameters.append(parse_filter_bound(from_value, label="--from"))
    if to_value:
        where.append("e.occurred_at <= ?")
        parameters.append(parse_filter_bound(to_value, end=True, label="--to"))
    if event_type:
        where.append("e.event_type = ?")
        parameters.append(event_type)
    if actor:
        where.append("e.actor = ?")
        parameters.append(actor)

    sql = f"""
        WITH candidate_ids(event_id) AS (
            SELECT CAST(event_id AS INTEGER)
            FROM events_fts
            WHERE events_fts MATCH ?
            UNION
            SELECT e2.id
            FROM events AS e2
            WHERE e2.content IS NOT NULL AND {like_conditions}
        )
        SELECT e.id AS event_id, e.session_id, e.sequence_no, e.event_type,
               e.actor, e.content, e.cwd, e.exit_code, e.occurred_at,
               e.sensitivity, s.source, s.title, s.model, s.started_at, s.initial_cwd
        FROM candidate_ids
        JOIN events AS e ON e.id = candidate_ids.event_id
        JOIN sessions AS s ON s.id = e.session_id
        {'WHERE ' + ' AND '.join(where) if where else ''}
        ORDER BY e.occurred_at, e.id
        LIMIT ?
    """
    parameters.append(limit)

    with connect_db(db_path) as connection:
        hit_rows = connection.execute(sql, parameters).fetchall()
        if not hit_rows:
            return []

        results: Dict[int, Dict[str, Any]] = {}
        for hit_row in hit_rows:
            hit = _event_dict(connection, hit_row, matched=True)
            results[hit["event_id"]] = hit

        for hit_row in hit_rows:
            start = max(1, int(hit_row["sequence_no"]) - context_before)
            finish = int(hit_row["sequence_no"]) + context_after
            for sequence_no in range(start, finish + 1):
                if sequence_no == int(hit_row["sequence_no"]):
                    continue
                context_row = _fetch_event(connection, hit_row["session_id"], sequence_no)
                if context_row is None:
                    continue
                event_id = int(context_row["event_id"])
                if event_id not in results:
                    results[event_id] = _event_dict(connection, context_row, matched=False)

        return sorted(results.values(), key=lambda item: (item["occurred_at"], item["event_id"]))


def render_search_results(results: List[Dict[str, Any]]) -> str:
    if not results:
        return "No matching events found."
    lines: List[str] = []
    for index, result in enumerate(results):
        marker = "match" if result["matched"] else "context"
        targets = ", ".join(result["targets"]) if result["targets"] else "-"
        lines.extend(
            [
                f"[{marker}] event_id={result['event_id']} session_id={result['session_id']}",
                f"  sequence={result['sequence_no']} time={result['occurred_at']}",
                f"  source={result['source']} type={result['event_type']} actor={result['actor']}",
                f"  targets={targets}",
                result["content"] if result["content"] is not None else "  (structured JSON only)",
            ]
        )
        if index != len(results) - 1:
            lines.append("")
    return "\n".join(lines)
