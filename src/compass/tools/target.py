"""Turn the `symbol` and `path` arguments an agent passes into index rows.

A symbol can be named as:
  path:line         the innermost symbol whose line range contains that line
                    ('model/Order.java:29'; any unique path suffix works)
  qualified name    'com.example.shop.model.Order.add', or any dotted suffix of it
                    ('Order.add', the short form every tool prints)
  simple name       'add'

There are no numeric ids. Row ids change when a file is reindexed and SQLite may
reuse them, so an id held by an agent across an edit could silently point at a
different symbol (docs/adr/005-tool-output-format.md).

When the argument does not pick out exactly one row, the result carries a short
message for the agent instead: the candidates to choose from, or the closest names.
"""

import os
import re
import sqlite3
from dataclasses import dataclass

from compass.indexer.models import TYPE_KINDS
from compass.store.db import get_meta
from compass.store.queries import (
    FileRow,
    SymbolRow,
    exact_symbols,
    file_symbols,
    files_by_suffix,
    fts_symbols,
)
from compass.tools.rank import short_name, symbol_line, symbol_rank_key
from compass.tools.render import Entry, clip, fit_grouped, truncated

_PATH_LINE = re.compile(r"(.+?):(\d+)(?::\d+)?")
MAX_CANDIDATES = 10


@dataclass(frozen=True)
class SymbolTarget:
    symbol: SymbolRow | None
    message: str | None = None  # set when symbol is None


@dataclass(frozen=True)
class FileTarget:
    file: FileRow | None
    message: str | None = None  # set when file is None


def normalize_path(conn: sqlite3.Connection, path: str) -> str:
    """Forward slashes, no leading './', and relative to the repo root if absolute."""
    path = path.strip().replace("\\", "/")
    root = (get_meta(conn, "repo_root") or "").replace("\\", "/").rstrip("/")
    if root and os.path.normcase(path).startswith(os.path.normcase(root + "/")):
        path = path[len(root) + 1 :]
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/")


def resolve_file(conn: sqlite3.Connection, path: str) -> FileTarget:
    spec = normalize_path(conn, path)
    if not spec:
        return FileTarget(None, "Empty path. Pass a repo-relative path or a unique suffix of one.")
    files = files_by_suffix(conn, spec)
    if len(files) == 1:
        return FileTarget(files[0])
    if not files:
        name = spec.rsplit("/", 1)[-1]
        similar = [f.path for f in files_by_suffix(conn, name)][:3] if name != spec else []
        hint = f" Same file name: {', '.join(similar)}." if similar else ""
        return FileTarget(
            None,
            clip(
                f'No indexed file matches "{spec}".{hint} Only .java and .py files are indexed;'
                " ignored, vendored and build directories are skipped.",
                500,
            ),
        )
    head = [f'"{spec}" matches {len(files)} files; pass more of the path:']
    lines = [f.path for f in files[:MAX_CANDIDATES]]
    if len(files) > MAX_CANDIDATES:
        lines.append(truncated(len(files) - MAX_CANDIDATES))
    return FileTarget(None, "\n".join(head + lines))


def resolve_symbol(conn: sqlite3.Connection, spec: str, cap_tokens: int) -> SymbolTarget:
    spec = spec.strip()
    located = _PATH_LINE.fullmatch(spec)
    if located:
        return _symbol_at(conn, located.group(1), int(located.group(2)))

    matches = exact_symbols(conn, spec)
    if len(matches) > 1:
        # 'com.x.Order' names the class exactly; do not let 'com.x.a.com.x.Order' compete.
        full = [s for s in matches if s.qualified_name == spec]
        matches = _type_over_constructors(full or matches)
    if len(matches) == 1:
        return SymbolTarget(matches[0])
    if matches:
        return SymbolTarget(None, ambiguous(spec, matches, cap_tokens))

    similar = fts_symbols(conn, spec, limit=3)
    message = f'No symbol "{spec}" in the index.'
    if similar:
        message += " Closest: " + ", ".join(
            f"{short_name(s)} ({s.path}:{s.start_line})" for s in similar
        )
        message += "."
    message += " search_symbols finds names by words."
    return SymbolTarget(None, clip(message, 500))


def ambiguous(spec: str, matches: list[SymbolRow], cap_tokens: int) -> str:
    ranked = sorted(matches, key=symbol_rank_key)
    head = [
        f'"{spec}" matches {len(matches)} symbols. Pass one as path:line or a longer name:',
    ]
    entries = [Entry(s.path, symbol_line(s)) for s in ranked[:MAX_CANDIDATES]]
    lines, shown = fit_grouped(head, entries, cap_tokens)
    if shown < len(ranked):
        lines.append(truncated(len(ranked) - shown))
    return "\n".join(lines)


def _type_over_constructors(matches: list[SymbolRow]) -> list[SymbolRow]:
    """A Java class and its own constructors share a name; 'Order' means the class."""
    types = [s for s in matches if s.kind in TYPE_KINDS]
    if len(types) == 1 and all(
        s is types[0] or (s.kind == "constructor" and s.parent_id == types[0].id) for s in matches
    ):
        return types
    return matches


def _symbol_at(conn: sqlite3.Connection, path: str, line: int) -> SymbolTarget:
    found = resolve_file(conn, path)
    if found.file is None:
        return SymbolTarget(None, found.message)
    containing = [
        s for s in file_symbols(conn, found.file.id) if s.start_line <= line <= s.end_line
    ]
    if not containing:
        return SymbolTarget(
            None,
            f"{found.file.path}:{line} is outside every symbol (module level: imports,"
            " constants). Read the file to see that line; file_outline lists what it defines.",
        )
    # Innermost: the latest start, then the shortest range.
    best = max(containing, key=lambda s: (s.start_line, s.start_line - s.end_line))
    return SymbolTarget(best)
