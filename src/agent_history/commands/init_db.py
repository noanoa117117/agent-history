"""Database initialization command."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..db import PathLike, connect_db, fts5_available, get_db_path, init_database


def run(db_path: Optional[PathLike] = None) -> str:
    path = init_database(db_path)
    with connect_db(path) as connection:
        available = fts5_available(connection)
    if not available:
        raise RuntimeError("SQLite FTS5 is unavailable")
    return f"Database: {Path(path)}\nFTS5: available"
