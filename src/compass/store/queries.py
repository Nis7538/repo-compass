"""Read-side queries over the index: typed rows and symbol lookup."""

import json
import sqlite3
from dataclasses import dataclass

from compass.store.db import split_words


@dataclass(frozen=True)
class SymbolRow:
    id: int
    file_id: int
    path: str
    language: str
    module: str  # of the file: Java package / Python module
    parent_id: int | None
    kind: str
    name: str
    qualified_name: str
    signature: str
    param_count: int | None
    is_varargs: bool
    decorators: tuple[str, ...]
    bases: tuple[str, ...]
    doc: str | None
    start_line: int
    end_line: int


@dataclass(frozen=True)
class RefRow:
    id: int
    file_id: int
    path: str
    language: str
    module: str
    enclosing_symbol_id: int | None
    kind: str
    name: str
    receiver: str | None
    arg_count: int | None
    line: int
    col: int


@dataclass(frozen=True)
class ImportRow:
    module: str
    name: str | None
    alias: str | None
    is_static: bool


SYMBOL_SELECT = (
    "SELECT s.*, f.path, f.language, f.module FROM symbols s JOIN files f ON f.id = s.file_id"
)
REF_SELECT = (
    "SELECT r.*, f.path, f.language, f.module FROM refs r JOIN files f ON f.id = r.file_id"
)

# Stable ordering for listings: by location.
_ORDER = " ORDER BY f.path, s.start_line, s.id"


def symbol_row(r: sqlite3.Row) -> SymbolRow:
    return SymbolRow(
        id=r["id"],
        file_id=r["file_id"],
        path=r["path"],
        language=r["language"],
        module=r["module"],
        parent_id=r["parent_id"],
        kind=r["kind"],
        name=r["name"],
        qualified_name=r["qualified_name"],
        signature=r["signature"],
        param_count=r["param_count"],
        is_varargs=bool(r["is_varargs"]),
        decorators=tuple(json.loads(r["decorators"])),
        bases=tuple(json.loads(r["bases"])),
        doc=r["doc"],
        start_line=r["start_line"],
        end_line=r["end_line"],
    )


def ref_row(r: sqlite3.Row) -> RefRow:
    return RefRow(
        id=r["id"],
        file_id=r["file_id"],
        path=r["path"],
        language=r["language"],
        module=r["module"],
        enclosing_symbol_id=r["enclosing_symbol_id"],
        kind=r["kind"],
        name=r["name"],
        receiver=r["receiver"],
        arg_count=r["arg_count"],
        line=r["line"],
        col=r["col"],
    )


def get_symbol(conn: sqlite3.Connection, symbol_id: int) -> SymbolRow | None:
    row = conn.execute(SYMBOL_SELECT + " WHERE s.id = ?", (symbol_id,)).fetchone()
    return symbol_row(row) if row else None


def find_symbols(
    conn: sqlite3.Connection, query: str, kind: str | None = None, fts_limit: int = 200
) -> list[SymbolRow]:
    """Look a symbol up by name, most precise match first.

    1. 'Order.add' style queries (containing a dot) match the qualified name exactly
       or as a dotted suffix; plain names match the simple name exactly.
    2. If nothing matched, a full-text prefix search over name, qualified name and
       the split words of the name ('getUser' also finds 'get_user_by_id').
    """
    kind_sql, kind_args = (" AND s.kind = ?", [kind]) if kind else ("", [])
    if "." in query:
        suffix = "%." + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = conn.execute(
            SYMBOL_SELECT
            + " WHERE (s.qualified_name = ? OR s.qualified_name LIKE ? ESCAPE '\\')"
            + kind_sql
            + _ORDER,
            [query, suffix, *kind_args],
        ).fetchall()
    else:
        rows = conn.execute(
            SYMBOL_SELECT + " WHERE s.name = ?" + kind_sql + _ORDER, [query, *kind_args]
        ).fetchall()
    if rows:
        return [symbol_row(r) for r in rows]

    words = split_words(query.rsplit(".", 1)[-1])
    if not words:
        return []
    match = " ".join(f'"{w}"*' for w in words.split())
    rows = conn.execute(
        SYMBOL_SELECT
        + " JOIN symbols_fts ON symbols_fts.rowid = s.id WHERE symbols_fts MATCH ?"
        + kind_sql
        + " ORDER BY bm25(symbols_fts), length(s.name), s.id LIMIT ?",
        [match, *kind_args, fts_limit],
    ).fetchall()
    return [symbol_row(r) for r in rows]
