"""argparse-based command line interface."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, Optional, Sequence

from .commands import CommandError
from .commands import event as event_commands
from .commands import claude_hook as claude_hook_commands
from .commands import export as export_commands
from .commands import init_db
from .commands import search as search_commands
from .commands import session as session_commands
from .commands import target as target_commands
from .db import get_db_path


def _add_context_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--context-before", type=int, default=0)
    parser.add_argument("--context-after", type=int, default=0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-history",
        description="Store and search observable AI and human work history in SQLite.",
    )
    parser.add_argument(
        "--db",
        help="SQLite path (otherwise AGENT_HISTORY_DB or data/agent_history.db)",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("init", help="initialize the SQLite database")

    start = subparsers.add_parser("session-start", help="register a new session")
    start.add_argument("--source", required=True)
    start.add_argument("--source-session-id")
    start.add_argument("--model")
    start.add_argument("--cwd", dest="initial_cwd")
    start.add_argument("--host-name")
    start.add_argument("--title")
    start.add_argument("--metadata-json")
    start.add_argument("--verbose", action="store_true")

    end = subparsers.add_parser("session-end", help="end a session")
    end.add_argument("session_id")

    session_list = subparsers.add_parser("session-list", help="list sessions")
    session_list.add_argument("--source")
    session_list.add_argument("--limit", type=int, default=20)

    show = subparsers.add_parser("session-show", help="show a session and its events")
    show.add_argument("session_id")

    event = subparsers.add_parser("event-add", help="append an event to a session")
    event.add_argument("--session", dest="session_id", required=True)
    event.add_argument("--type", dest="event_type", required=True)
    event.add_argument("--actor", required=True)
    content_group = event.add_mutually_exclusive_group()
    content_group.add_argument("--content")
    content_group.add_argument("--content-file")
    event.add_argument("--content-json")
    event.add_argument("--cwd")
    event.add_argument("--exit-code", type=int)
    event.add_argument("--occurred-at")
    event.add_argument("--no-sanitize", action="store_true")

    target = subparsers.add_parser("target-add", help="register a work target")
    target.add_argument("--type", dest="target_type", required=True)
    target.add_argument("--slug", required=True)
    target.add_argument("--name", required=True)
    target.add_argument("--locator")
    target.add_argument("--metadata-json")
    target.add_argument("--update", action="store_true")

    target_list = subparsers.add_parser("target-list", help="list work targets")
    target_list.add_argument("--type", dest="target_type")

    relation = subparsers.add_parser(
        "session-target-add", help="relate a session to a target"
    )
    relation.add_argument("--session", dest="session_id", required=True)
    relation.add_argument("--target", dest="target_id", required=True, type=int)
    relation.add_argument("--relation", dest="relation_type", default="related")
    relation.add_argument("--confidence", type=float)
    relation.add_argument("--assigned-by", default="manual")

    search = subparsers.add_parser("search", help="search event content with SQLite FTS5")
    search.add_argument("query")
    search.add_argument("--source")
    search.add_argument("--session", dest="session_id")
    search.add_argument("--target")
    search.add_argument("--from", dest="from_value")
    search.add_argument("--to", dest="to_value")
    search.add_argument("--event-type")
    search.add_argument("--actor")
    search.add_argument("--limit", type=int, default=20)
    _add_context_options(search)
    search.add_argument("--json", action="store_true", dest="as_json")

    export = subparsers.add_parser("export", help="export search results as Markdown")
    export.add_argument("--query", required=True)
    export.add_argument("--source")
    export.add_argument("--target")
    export.add_argument("--from", dest="from_value")
    export.add_argument("--to", dest="to_value")
    export.add_argument("--session", dest="session_id")
    _add_context_options(export)
    export.add_argument("--output")

    # These subcommands deliberately do not declare their own --db: a subparser
    # option of the same name overwrites the global one with its own default,
    # which silently pointed them at the default database.
    hook_status = subparsers.add_parser(
        "claude-hook-status", help="show Claude Code hook installation and capture status"
    )
    hook_status.add_argument("--scope", choices=("user", "project", "local"), default="user")

    claude_sessions = subparsers.add_parser(
        "claude-sessions", help="list sessions captured from Claude Code"
    )
    claude_sessions.add_argument("--limit", type=int, default=20)

    return parser


def _run(args: argparse.Namespace) -> str:
    db_path = args.db
    if args.command == "init":
        return init_db.run(db_path)
    if args.command == "session-start":
        session_id = session_commands.create_session(
            db_path,
            source=args.source,
            source_session_id=args.source_session_id,
            model=args.model,
            initial_cwd=args.initial_cwd,
            host_name=args.host_name,
            title=args.title,
            metadata_json=args.metadata_json,
        )
        if args.verbose:
            return f"Session ID: {session_id}\nSource: {args.source}"
        return session_id
    if args.command == "session-end":
        ended = session_commands.end_session(db_path, args.session_id)
        return (
            f"Session ended: {args.session_id}"
            if ended
            else f"Session already ended: {args.session_id}"
        )
    if args.command == "session-list":
        rows = session_commands.list_sessions(db_path, source=args.source, limit=args.limit)
        return session_commands.render_session_list(rows)
    if args.command == "session-show":
        return session_commands.render_session_show(
            session_commands.show_session(db_path, args.session_id)
        )
    if args.command == "event-add":
        event_id = event_commands.add_event(
            db_path,
            session_id=args.session_id,
            event_type=args.event_type,
            actor=args.actor,
            content=args.content,
            content_file=args.content_file,
            content_json=args.content_json,
            cwd=args.cwd,
            exit_code=args.exit_code,
            occurred_at=args.occurred_at,
            no_sanitize=args.no_sanitize,
        )
        return str(event_id)
    if args.command == "target-add":
        target_id = target_commands.add_target(
            db_path,
            target_type=args.target_type,
            slug=args.slug,
            name=args.name,
            locator=args.locator,
            metadata_json=args.metadata_json,
            update=args.update,
        )
        return str(target_id)
    if args.command == "target-list":
        rows = target_commands.list_targets(db_path, target_type=args.target_type)
        return target_commands.render_target_list(rows)
    if args.command == "session-target-add":
        target_commands.add_session_target(
            db_path,
            session_id=args.session_id,
            target_id=args.target_id,
            relation_type=args.relation_type,
            confidence=args.confidence,
            assigned_by=args.assigned_by,
        )
        return (
            f"Linked session {args.session_id} to target {args.target_id} "
            f"({args.relation_type})"
        )
    if args.command == "search":
        results = search_commands.search_events(
            db_path,
            args.query,
            source=args.source,
            session_id=args.session_id,
            target=args.target,
            from_value=args.from_value,
            to_value=args.to_value,
            event_type=args.event_type,
            actor=args.actor,
            limit=args.limit,
            context_before=args.context_before,
            context_after=args.context_after,
        )
        if args.as_json:
            return json.dumps(results, ensure_ascii=False, indent=2)
        return search_commands.render_search_results(results)
    if args.command == "export":
        return export_commands.export_markdown(
            db_path,
            query=args.query,
            source=args.source,
            target=args.target,
            from_value=args.from_value,
            to_value=args.to_value,
            session_id=args.session_id,
            context_before=args.context_before,
            context_after=args.context_after,
            output=args.output,
        )
    if args.command == "claude-hook-status":
        return claude_hook_commands.status(db_path=db_path, scope=args.scope)
    if args.command == "claude-sessions":
        return claude_hook_commands.claude_sessions(db_path, limit=args.limit)
    raise CommandError("a command is required; use --help for usage")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return 2
    try:
        output = _run(args)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            print(
                f"error: database is not initialized ({get_db_path(args.db)}); "
                "run `agent-history init` first",
                file=sys.stderr,
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    except (CommandError, ValueError, RuntimeError, sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if output:
        print(output)
    return 0
