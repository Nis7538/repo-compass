"""file_outline: what a file defines, without reading it.

Also provides member_outline, which get_symbol uses for classes.

Ranking for truncation: types first, then methods/functions/constructors, then
fields; ties by source order. The kept entries are printed as a tree in source
order, indented by nesting depth.
"""

import sqlite3
from collections import Counter

from compass.store.queries import SymbolRow, file_symbols
from compass.tools.caps import CAPS, MAX_LIMIT
from compass.tools.rank import SIGNATURE_WIDTH, kind_rank
from compass.tools.render import clip, fit_lines, more_hint, truncated
from compass.tools.target import resolve_file

_PLURAL = {"class": "classes"}


def file_outline(conn: sqlite3.Connection, path: str, limit: int) -> str:
    found = resolve_file(conn, path)
    if found.file is None:
        return found.message
    f = found.file
    head = [f"{f.path} ({f.language}, {f.line_count} lines, module {f.module or '-'})"]
    if f.has_errors:
        head.append("parse errors: yes (tree-sitter recovered; some symbols may be missing)")
    symbols = file_symbols(conn, f.id)
    if not symbols:
        return "\n".join(head + ["no symbols"])
    return "\n".join(member_outline(head, symbols, None, limit, CAPS["file_outline"]))


def member_outline(
    head: list[str], symbols: list[SymbolRow], root_id: int | None, limit: int, cap: int
) -> list[str]:
    """Head + an indented tree of the symbols below root_id (None: the whole file)."""
    by_id = {s.id: s for s in symbols}

    def depth(s: SymbolRow) -> int | None:
        """Nesting depth below root_id, or None if s is not below it."""
        d = 0
        parent = s.parent_id
        while parent != root_id:
            if parent is None or parent not in by_id:
                return None
            d += 1
            parent = by_id[parent].parent_id
        return d

    members = [(s, d) for s in symbols if (d := depth(s)) is not None]
    ranked = sorted(members, key=lambda m: (kind_rank(m[0].kind), m[0].start_line, m[0].id))
    chosen = ranked[:limit]
    # Fit against the cap in rank order, then print the survivors in source order.
    _, fitted = fit_lines(head, [_line(s, d) for s, d in chosen], cap)
    kept = sorted(chosen[:fitted], key=lambda m: (m[0].start_line, m[0].id))
    lines = head + [_line(s, d) for s, d in kept]

    dropped = [s for s, _ in ranked[fitted:]]
    if dropped:
        counts = Counter(_PLURAL.get(s.kind, s.kind + "s") for s in dropped)
        detail = ", ".join(f"{kind} {n}" for kind, n in counts.most_common())
        hint = more_hint(len(ranked), fitted, limit, MAX_LIMIT)
        lines.append(truncated(len(dropped), detail, hint))
    return lines


def _line(s: SymbolRow, depth: int) -> str:
    span = str(s.start_line) if s.start_line == s.end_line else f"{s.start_line}-{s.end_line}"
    # The signature already contains the name. The kind tells fields from methods, and is
    # dropped when the signature spells it out ('class Line', 'enum Channel').
    signature = clip(s.signature, SIGNATURE_WIDTH)
    keyword = "@interface" if s.kind == "annotation" else s.kind
    kind = "" if keyword in signature.split() else s.kind + " "
    return f"{'  ' * (depth + 1)}{span} {kind}{signature}"
