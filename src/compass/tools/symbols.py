"""search_symbols and get_symbol.

search_symbols returns locations and signatures only. Bodies come from
get_symbol, one symbol at a time (progressive disclosure).

search ranking, best first:
  1. exact matches: simple name equal to the query, or qualified name equal to /
     ending in '.query' when it contains a dot;
  2. word matches from the full-text index (bm25; 'getUser' finds get_user_by_id).
Within the exact tier: production code before tests, types before members,
then location. Within the word tier: bm25 order, tests moved after non-tests.

`path` limits the search to a directory, file or package (store.queries.Scope).
With an empty query it lists what that scope defines, ranked like the exact
tier: production before tests, types, then callables, then fields. That answers
"what are the main classes of package X" without reading its files.
"""

import difflib
import re
import sqlite3
import textwrap
from collections import Counter
from pathlib import Path

from compass.indexer.models import TYPE_KINDS
from compass.store.db import get_meta
from compass.store.queries import (
    SOURCE_SUFFIXES,
    FileRow,
    Scope,
    SymbolRow,
    exact_symbols,
    file_symbols,
    fts_symbols,
    get_file,
    scope_files,
    scope_of,
    scope_symbols,
)
from compass.tools.caps import CAPS, MAX_LIMIT
from compass.tools.outline import member_outline
from compass.tools.rank import (
    is_test_path,
    kind_plural,
    short_name,
    symbol_line,
    symbol_rank_key,
)
from compass.tools.render import Entry, clip, fit_grouped, fit_lines, more_hint, truncated
from compass.tools.target import normalize_path, resolve_symbol

KINDS = (
    "class",
    "interface",
    "enum",
    "record",
    "annotation",
    "method",
    "constructor",
    "function",
    "field",
)
FTS_LIMIT = 200
BODY_LINE_WIDTH = 200
DOC_WIDTH = 300


def search_symbols(
    conn: sqlite3.Connection, query: str, kind: str | None, limit: int, path: str | None = None
) -> str:
    query = query.strip()
    if kind is not None and kind not in KINDS:
        return f'Unknown kind "{kind}". Use one of: {", ".join(KINDS)}.'
    scope = None
    if path is not None and path.strip():
        scope = _scope(conn, path)
        files = scope_files(conn, scope)
        if not files:
            return _no_such_scope(conn, scope)
        if not query:
            return _list_scope(conn, scope, files, kind, limit)
    if not query:
        return (
            "Empty query. Pass a name ('add'), a dotted name ('Order.add') or words"
            " ('get user'); or a path ('model/') and no query to list what it defines."
        )

    exact = sorted(exact_symbols(conn, query, kind, scope), key=symbol_rank_key)
    seen = {s.id for s in exact}
    fts = fts_symbols(conn, query, kind, FTS_LIMIT, scope)
    words = [s for s in fts if s.id not in seen]
    words.sort(key=lambda s: is_test_path(s.path))  # stable: keeps bm25 order otherwise
    ranked = exact + words
    about = (f" kind={kind}" if kind else "") + (f" under {scope.spec}" if scope else "")
    if not ranked:
        return f'No symbols match "{query}"{about}.'

    more = "+" if len(fts) == FTS_LIMIT else ""  # the word search stopped early
    noun = "symbol matches" if len(ranked) == 1 else "symbols match"
    tiers = f"exact {len(exact)}" + (f", word match {len(words)}{more}" if words else "")
    head = [f'{len(ranked)}{more} {noun} "{query}"{about} ({tiers})']
    entries = [Entry(s.path, symbol_line(s)) for s in ranked[:limit]]
    lines, shown = fit_grouped(head, entries, CAPS["search_symbols"])
    if shown < len(ranked):
        hint = more_hint(len(ranked), shown, limit, MAX_LIMIT)
        lines.append(truncated(len(ranked) - shown, None, hint))
    return "\n".join(lines)


def _scope(conn: sqlite3.Connection, path: str) -> Scope:
    """normalize_path strips slashes, but a trailing '/' means "this is a directory"."""
    spec = normalize_path(conn, path)
    if path.strip().endswith(("/", "\\")) and "/" not in spec:
        spec += "/"
    return scope_of(spec)


def _list_scope(
    conn: sqlite3.Connection, scope: Scope, files: list[FileRow], kind: str | None, limit: int
) -> str:
    """Everything a scope defines, types first, under a header with counts per kind."""
    symbols = sorted(scope_symbols(conn, scope, kind), key=symbol_rank_key)
    where = f" under {scope.spec} ({len(files)} file{'s' if len(files) != 1 else ''})"
    about = f" kind={kind}" if kind else ""
    if not symbols:
        return f"No symbols{about}{where}."
    head = f"{len(symbols)} symbol{'s' if len(symbols) != 1 else ''}{about}{where}"
    if kind is None:
        counts = Counter(s.kind for s in symbols)
        head += ": " + ", ".join(f"{k} {counts[k]}" for k in KINDS if counts[k])
    entries = [Entry(s.path, symbol_line(s)) for s in symbols[:limit]]
    lines, shown = fit_grouped([head], entries, CAPS["search_symbols"])
    if shown < len(symbols):
        dropped = Counter(kind_plural(s.kind) for s in symbols[shown:])
        detail = ", ".join(f"{k} {n}" for k, n in dropped.most_common())
        hint = more_hint(len(symbols), shown, limit, MAX_LIMIT)
        lines.append(truncated(len(symbols) - shown, detail, hint))
    return "\n".join(lines)


def _no_such_scope(conn: sqlite3.Connection, scope: Scope) -> str:
    """Suggest directory, file or module names close to the last segment asked for."""
    names: set[str] = set()
    for r in conn.execute("SELECT path, module FROM files"):
        *dirs, name = r["path"].split("/")
        names.update(dirs)
        names.add(name.rsplit(".", 1)[0])
        names.update(r["module"].split(".") if r["module"] else [])
    stem = scope.spec
    for suffix in SOURCE_SUFFIXES:
        stem = stem.removesuffix(suffix)
    last = re.split(r"[/.]", stem.strip("/."))[-1]
    close = difflib.get_close_matches(last, sorted(names), n=3, cutoff=0.75)
    hint = f" Similar names: {', '.join(close)}." if close else ""
    return clip(
        f'No indexed file under "{scope.spec}".{hint} Pass a directory ("model/"), a file'
        ' ("Order.java") or a package or module ("com.x.model", "inventory.models").',
        500,
    )


def get_symbol(conn: sqlite3.Connection, symbol: str, max_lines: int) -> str:
    cap = CAPS["get_symbol"]
    target = resolve_symbol(conn, symbol, cap)
    if target.symbol is None:
        return target.message
    s = target.symbol
    head = [f"{s.kind} {short_name(s)} @ {s.path}:{s.start_line}-{s.end_line}"]
    if s.decorators:
        head.append("decorators: " + clip(", ".join(s.decorators), DOC_WIDTH))

    if s.kind in TYPE_KINDS:
        head.append(clip(s.signature, DOC_WIDTH))
        if s.doc:
            head.append("doc: " + clip(s.doc, DOC_WIDTH))
        members = file_symbols(conn, s.file_id)
        lines = member_outline(head, members, s.id, max_lines, cap)
        if len(lines) == len(head):
            lines.append("no members")
        return "\n".join(lines)

    if s.doc and s.language == "java":  # Python docstrings are part of the body already
        head.append("doc: " + clip(s.doc, DOC_WIDTH))
    body, note = _read_lines(conn, s)
    if note:
        head.append(note)
    if body is None:
        head.append(clip(s.signature, DOC_WIDTH))
        return "\n".join(head)

    body = [clip_code(line) for line in textwrap.dedent("\n".join(body)).split("\n")]
    wanted = body[:max_lines]
    lines, shown = fit_lines(head, wanted, cap)
    if shown < len(body):
        first = s.start_line + shown
        lines.append(
            truncated(
                len(body) - shown, None, f"read {s.path} lines {first}-{s.end_line}", "lines"
            )
        )
    return "\n".join(lines)


def clip_code(line: str) -> str:
    """Cut an over-long source line, keeping its indentation."""
    line = line.rstrip()
    if len(line) <= BODY_LINE_WIDTH:
        return line
    return line[: BODY_LINE_WIDTH - 1] + "…"


def _read_lines(conn: sqlite3.Connection, s: SymbolRow) -> tuple[list[str] | None, str | None]:
    """Source lines start..end (1-based, inclusive) and an optional note for the agent."""
    root = get_meta(conn, "repo_root")
    if root is None:
        return None, "[source unavailable: index has no repo root]"
    file_path = Path(root) / s.path
    try:
        stat = file_path.stat()
        data = file_path.read_bytes()
    except OSError:
        return None, "[file no longer on disk; showing the indexed signature only]"
    note = None
    indexed = get_file(conn, s.file_id)
    if indexed is not None and (stat.st_size, stat.st_mtime_ns) != (
        indexed.size,
        indexed.mtime_ns,
    ):
        note = "[file changed since it was indexed; line numbers may be off]"
    text = data.decode("utf-8", errors="replace")
    # Split on \n only: tree-sitter line numbers count \n, and str.splitlines would
    # also split on form feeds and other separators.
    lines = [line.rstrip("\r") for line in text.split("\n")]
    return lines[s.start_line - 1 : s.end_line], note
