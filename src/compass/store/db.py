"""Opening the index database and writing per-file rows."""

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from compass.indexer.models import FileExtract
from compass.store.schema import DDL, SCHEMA_VERSION, TABLES, TRIGGERS


class IndexNotFoundError(Exception):
    """No usable index exists at the given path."""


@dataclass(frozen=True)
class FileState:
    """What the index remembers about a file, used for change detection."""

    id: int
    size: int
    mtime_ns: int
    hash: str
    module: str


@dataclass(frozen=True)
class FileRecord:
    """A file about to be written to the index."""

    path: str
    language: str
    hash: str
    size: int
    mtime_ns: int
    line_count: int


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the index at db_path, rebuilding it on a schema change."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open(db_path)
    if _schema_version(conn) != SCHEMA_VERSION:
        _rebuild(conn)
    return conn


def open_index(db_path: Path) -> sqlite3.Connection:
    """Open an existing index for querying. Never creates or rebuilds anything."""
    if not db_path.is_file():
        raise IndexNotFoundError(f"no index at {db_path}")
    conn = _open(db_path)
    if _schema_version(conn) != SCHEMA_VERSION:
        conn.close()
        raise IndexNotFoundError(f"index at {db_path} is from another version; reindex")
    return conn


def _open(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _schema_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:  # no meta table: empty or foreign database
        return None
    return int(row["value"]) if row else None


def _rebuild(conn: sqlite3.Connection) -> None:
    # Triggers first: dropping `symbols` would otherwise fire the FTS delete trigger.
    for trigger in TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    for table in TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(DDL)
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def load_file_states(conn: sqlite3.Connection) -> dict[str, FileState]:
    rows = conn.execute("SELECT id, path, size, mtime_ns, hash, module FROM files")
    return {
        r["path"]: FileState(r["id"], r["size"], r["mtime_ns"], r["hash"], r["module"])
        for r in rows
    }


def delete_file(conn: sqlite3.Connection, file_id: int) -> None:
    """Remove a file and, via ON DELETE CASCADE, all of its symbols, imports and refs."""
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))


def update_file_stat(conn: sqlite3.Connection, file_id: int, size: int, mtime_ns: int) -> None:
    conn.execute("UPDATE files SET size = ?, mtime_ns = ? WHERE id = ?", (size, mtime_ns, file_id))


def write_file(conn: sqlite3.Connection, record: FileRecord, extract: FileExtract) -> int:
    """Replace everything stored for record.path with the rows in extract. Returns file id."""
    conn.execute("DELETE FROM files WHERE path = ?", (record.path,))
    file_id = conn.execute(
        "INSERT INTO files(path, language, module, hash, size, mtime_ns, line_count, has_errors)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            record.path,
            record.language,
            extract.module,
            record.hash,
            record.size,
            record.mtime_ns,
            record.line_count,
            int(extract.has_errors),
        ),
    ).lastrowid

    # Extractors emit parents before children, so parent ids are always known here.
    symbol_ids: list[int] = []
    for s in extract.symbols:
        parent_id = symbol_ids[s.parent] if s.parent is not None else None
        cur = conn.execute(
            "INSERT INTO symbols(file_id, parent_id, kind, name, qualified_name, signature,"
            " param_count, is_varargs, decorators, bases, doc, name_words, start_line, end_line)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                file_id,
                parent_id,
                s.kind,
                s.name,
                s.qualified_name,
                s.signature,
                s.param_count,
                int(s.is_varargs),
                json.dumps(s.decorators),
                json.dumps(s.bases),
                s.doc,
                split_words(s.name),
                s.start_line,
                s.end_line,
            ),
        )
        symbol_ids.append(cur.lastrowid)

    conn.executemany(
        "INSERT INTO imports(file_id, module, name, alias, is_static, line)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(file_id, i.module, i.name, i.alias, int(i.is_static), i.line) for i in extract.imports],
    )
    conn.executemany(
        "INSERT INTO refs(file_id, enclosing_symbol_id, kind, name, receiver, arg_count,"
        " line, col) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                file_id,
                symbol_ids[r.enclosing] if r.enclosing is not None else None,
                r.kind,
                r.name,
                r.receiver,
                r.arg_count,
                r.line,
                r.col,
            )
            for r in extract.refs
        ],
    )
    return file_id


_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def split_words(name: str) -> str:
    """'getUserByID' -> 'get user by id', 'parse_http_url' -> 'parse http url'."""
    return " ".join(w.lower() for w in _WORD.findall(name))
