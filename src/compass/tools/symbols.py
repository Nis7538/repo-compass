"""search_symbols and get_symbol.

search_symbols returns locations and signatures only. Bodies come from
get_symbol, one symbol at a time (progressive disclosure).

search ranking, best first:
  1. exact matches: simple name equal to the query, or qualified name equal to /
     ending in '.query' when it contains a dot;
  2. word matches from the full-text index (bm25; 'getUser' finds get_user_by_id).
Within the exact tier: production code before tests, types before members,
then location. Within the word tier: bm25 order, tests moved after non-tests.
"""

import sqlite3
import textwrap
from pathlib import Path

from compass.indexer.models import TYPE_KINDS
from compass.store.db import get_meta
from compass.store.queries import SymbolRow, exact_symbols, file_symbols, fts_symbols, get_file
from compass.tools.caps import CAPS, MAX_LIMIT
from compass.tools.outline import member_outline
from compass.tools.rank import is_test_path, short_name, symbol_line, symbol_rank_key
from compass.tools.render import Entry, clip, fit_grouped, fit_lines, more_hint, truncated
from compass.tools.target import resolve_symbol

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


def search_symbols(conn: sqlite3.Connection, query: str, kind: str | None, limit: int) -> str:
    query = query.strip()
    if not query:
        return (
            "Empty query. Pass a name ('add'), a dotted name ('Order.add') or words ('get user')."
        )
    if kind is not None and kind not in KINDS:
        return f'Unknown kind "{kind}". Use one of: {", ".join(KINDS)}.'

    exact = sorted(exact_symbols(conn, query, kind), key=symbol_rank_key)
    seen = {s.id for s in exact}
    fts = fts_symbols(conn, query, kind, FTS_LIMIT)
    words = [s for s in fts if s.id not in seen]
    words.sort(key=lambda s: is_test_path(s.path))  # stable: keeps bm25 order otherwise
    ranked = exact + words
    about = f" kind={kind}" if kind else ""
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
