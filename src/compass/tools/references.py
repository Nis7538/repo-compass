"""find_references: call sites of one symbol, with the calling symbol and one line of context.

Ranking, best first: confidence tier (exact > likely > possible, see
docs/adr/003-reference-resolution.md), then production code before tests, then
the first call site of each calling symbol before its repeats, then path and
line. The header always gives the full totals, so a truncated answer
still says how many call sites exist, in how many files, and how sure we are.
The truncation marker also names the files holding most of the cut call sites.
"""

import sqlite3
from collections import Counter
from pathlib import Path

from compass.store.db import get_meta
from compass.store.queries import get_symbol as get_symbol_row
from compass.store.resolve import EXACT, LIKELY, POSSIBLE, RANK, Reference, Resolver
from compass.tools.caps import CAPS, MAX_LIMIT
from compass.tools.rank import SIGNATURE_WIDTH, is_test_path, short_name
from compass.tools.render import Entry, clip, fit_grouped, more_hint, truncated
from compass.tools.target import resolve_symbol

CONTEXT_WIDTH = 80
TOP_FILES = 3


def find_references(conn: sqlite3.Connection, symbol: str, limit: int) -> str:
    cap = CAPS["find_references"]
    target = resolve_symbol(conn, symbol, cap)
    if target.symbol is None:
        return target.message
    s = target.symbol
    head = [
        f"{s.kind} {short_name(s)} @ {s.path}:{s.start_line}  {clip(s.signature, SIGNATURE_WIDTH)}"
    ]

    refs = rank_references(Resolver(conn).find_references(s))
    if not refs:
        head.append(
            "No call sites found. Only calls and `new` are indexed, not type usages,"
            " reflection or dependency injection (docs/adr/003)."
        )
        return "\n".join(head)

    files = {r.ref.path for r in refs}
    head.append(
        f"{len(refs)} call site{'s' if len(refs) != 1 else ''} in {len(files)} "
        f"file{'s' if len(files) != 1 else ''} ({_tiers(refs)})"
    )
    head.append("rank: exact > likely > possible, non-test first, new callers before repeat calls")

    wanted = refs[:limit]
    entries = [Entry(r.ref.path, text) for r, text in zip(wanted, _entry_texts(conn, wanted))]
    lines, shown = fit_grouped(head, entries, cap)
    if shown < len(refs):
        dropped = refs[shown:]
        counts = Counter(r.ref.path for r in dropped)
        top = ", ".join(
            f"{path.rsplit('/', 1)[-1]} {n}" for path, n in counts.most_common(TOP_FILES)
        )
        n_files = len(counts)
        detail = f"{_tiers(dropped)}; {n_files} file{'s' if n_files != 1 else ''}, most in {top}"
        lines.append(
            truncated(len(dropped), detail, more_hint(len(refs), shown, limit, MAX_LIMIT))
        )
    return "\n".join(lines)


def rank_references(refs: list[Reference]) -> list[Reference]:
    """Best first: tier, production before tests, first call from each caller, location.

    "Who calls X" is answered by distinct callers, so a method that calls X forty
    times must not fill the whole first page: its first call site ranks with the
    other callers' first ones, and its repeats come after them (same tier).
    """
    by_location = sorted(refs, key=lambda r: (r.ref.path, r.ref.line, r.ref.col))
    seen: Counter = Counter()
    repeat: dict[int, int] = {}
    for r in by_location:
        caller = (r.ref.file_id, r.ref.enclosing_symbol_id)
        repeat[r.ref.id] = seen[caller]
        seen[caller] += 1
    return sorted(
        by_location,
        key=lambda r: (
            -RANK[r.confidence],
            is_test_path(r.ref.path),
            repeat[r.ref.id] > 0,
            r.ref.path,
            r.ref.line,
        ),
    )


def _tiers(refs: list[Reference]) -> str:
    counts = Counter(r.confidence for r in refs)
    return ", ".join(
        f"{tier} {counts[tier]}" for tier in (EXACT, LIKELY, POSSIBLE) if counts[tier]
    )


def _entry_texts(conn: sqlite3.Connection, refs: list[Reference]) -> list[str]:
    """'61 exact Order.addAll: for (Item i : items) add(i);' for each reference."""
    root = get_meta(conn, "repo_root")
    sources: dict[str, list[str]] = {}
    callers: dict[int, str] = {}
    texts = []
    for r in refs:
        ref = r.ref
        if ref.path not in sources:
            sources[ref.path] = _source_lines(root, ref.path)
        lines = sources[ref.path]
        context = clip(lines[ref.line - 1], CONTEXT_WIDTH) if ref.line <= len(lines) else ""
        caller = "(top level)"
        if ref.enclosing_symbol_id is not None:
            if ref.enclosing_symbol_id not in callers:
                row = get_symbol_row(conn, ref.enclosing_symbol_id)
                callers[ref.enclosing_symbol_id] = short_name(row) if row else "?"
            caller = callers[ref.enclosing_symbol_id]
        texts.append(f"{ref.line} {r.confidence} {caller}: {context}".rstrip())
    return texts


def _source_lines(root: str | None, path: str) -> list[str]:
    if root is None:
        return []
    try:
        data = (Path(root) / path).read_bytes()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").split("\n")
