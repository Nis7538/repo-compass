"""Which symbols changed between two versions of a set of files, and how.

Both versions are parsed with the indexer's own extractors, so "symbol" means
exactly what the index means, and the answer is exact, not a guess from diff hunks.

Symbols are matched by (qualified name, kind) across all the files given, not
file by file, so a class moved to another file of the same package is the same
symbol. Overloads (same name and kind) are paired by identical signature first,
then in source order. Each symbol gets at most one change:

  removed    only in the base version
  added      only in the head version
  signature  the declaration differs: signature text, decorators/annotations,
             parameter count or varargs
  body       the symbol's own text differs. Own text is its lines minus the lines
             of its child symbols, with all whitespace removed. So a changed
             method does not also mark its class, and a method that was only
             moved, re-indented or reformatted is not reported (the cost: an edit
             that only adds or removes spaces inside a string literal is missed).
             For types, comment lines are left out too, so editing a member's
             Javadoc does not mark the class; inside a method a comment edit
             does count.

Module-level code that is not a symbol (imports, constants, statements) is not
reported; the file still counts as changed.
"""

from collections import defaultdict
from dataclasses import dataclass

from compass.indexer.extract_java import extract_java
from compass.indexer.extract_python import extract_python
from compass.indexer.models import TYPE_KINDS, FileExtract, Symbol

REMOVED = "removed"
ADDED = "added"
SIGNATURE = "signature"
BODY = "body"
_COMMENT_STARTS = ("//", "/*", "*", "#")


@dataclass(frozen=True)
class Version:
    """One file as it is at one side of the diff."""

    path: str
    language: str
    module: str  # Python module name at this version; unused for Java
    data: bytes


@dataclass(frozen=True)
class Parsed:
    path: str
    extract: FileExtract
    own: list[str]  # own text of each symbol, in the order of extract.symbols


@dataclass(frozen=True)
class SymbolChange:
    change: str  # removed | added | signature | body
    path: str  # where the symbol is in head; for removed, where it was in base
    module: str  # of that file: Java package / Python module
    symbol: Symbol  # the head version; the base version when removed
    parent: Symbol | None  # the enclosing symbol, from the same version
    was: Symbol | None = None  # the base version, for signature and body changes


def parse(version: Version) -> Parsed:
    if version.language == "java":
        extract = extract_java(version.data)
    else:
        is_package = version.path.endswith("__init__.py")
        extract = extract_python(version.data, version.module, is_package)
    lines = version.data.decode("utf-8", errors="replace").split("\n")
    return Parsed(version.path, extract, own_texts(extract.symbols, lines))


@dataclass(frozen=True)
class _Occurrence:
    file: Parsed
    index: int

    @property
    def symbol(self) -> Symbol:
        return self.file.extract.symbols[self.index]


def diff_symbols(base: list[Parsed], head: list[Parsed]) -> list[SymbolChange]:
    """Every changed symbol, ordered by path and line."""
    before = _occurrences(base)
    after = _occurrences(head)
    changes = []
    for key in before.keys() | after.keys():
        pairs, removed, added = _pair(before.get(key, []), after.get(key, []))
        for old, new in pairs:
            kind = _change_kind(old, new)
            if kind is not None:
                changes.append(_change(kind, new, was=old.symbol))
        changes.extend(_change(REMOVED, occ) for occ in removed)
        changes.extend(_change(ADDED, occ) for occ in added)
    return sorted(changes, key=lambda c: (c.path, c.symbol.start_line, c.symbol.qualified_name))


def _occurrences(files: list[Parsed]) -> dict[tuple[str, str], list[_Occurrence]]:
    found: dict[tuple[str, str], list[_Occurrence]] = defaultdict(list)
    for f in sorted(files, key=lambda f: f.path):
        for i, s in enumerate(f.extract.symbols):
            found[(s.qualified_name, s.kind)].append(_Occurrence(f, i))
    return found


def _declaration(s: Symbol) -> tuple:
    return (s.signature, tuple(s.decorators), s.param_count, s.is_varargs)


def _pair(old: list[_Occurrence], new: list[_Occurrence]):
    """Pair up occurrences of one (name, kind): same declaration first, then in order."""
    pairs = []
    old_left, new_left = list(old), list(new)
    for o in list(old_left):
        declared = _declaration(o.symbol)
        match = next((n for n in new_left if _declaration(n.symbol) == declared), None)
        if match is not None:
            pairs.append((o, match))
            old_left.remove(o)
            new_left.remove(match)
    n = min(len(old_left), len(new_left))
    pairs.extend(zip(old_left[:n], new_left[:n]))
    return pairs, old_left[n:], new_left[n:]


def _change_kind(old: _Occurrence, new: _Occurrence) -> str | None:
    if _declaration(old.symbol) != _declaration(new.symbol):
        return SIGNATURE
    if old.file.own[old.index] != new.file.own[new.index]:
        return BODY
    return None


def own_texts(symbols: list[Symbol], lines: list[str]) -> list[str]:
    """Each symbol's lines minus its children's lines, with all whitespace removed."""
    child_lines: dict[int, set[int]] = defaultdict(set)
    for child in symbols:
        if child.parent is not None:
            child_lines[child.parent].update(range(child.start_line, child.end_line + 1))
    texts = []
    for i, symbol in enumerate(symbols):
        skip_comments = symbol.kind in TYPE_KINDS
        kept = []
        for n in range(symbol.start_line, min(symbol.end_line, len(lines)) + 1):
            if n in child_lines[i]:
                continue
            line = lines[n - 1].strip()
            if skip_comments and line.startswith(_COMMENT_STARTS):
                continue
            kept.append(line)
        texts.append("".join("".join(kept).split()))
    return texts


def _change(kind: str, occ: _Occurrence, was: Symbol | None = None) -> SymbolChange:
    s = occ.symbol
    parent = occ.file.extract.symbols[s.parent] if s.parent is not None else None
    return SymbolChange(kind, occ.file.path, occ.file.extract.module, s, parent, was)
