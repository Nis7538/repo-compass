"""Small ranking and naming helpers shared by the tools."""

import re

from compass.indexer.models import TYPE_KINDS
from compass.store.queries import SymbolRow
from compass.tools.render import clip

SIGNATURE_WIDTH = 120
_TEST_DIRS = frozenset({"test", "tests", "testing", "it"})
_TEST_FILE = re.compile(r"(^test_.*\.py|.*_test\.py|conftest\.py|.*(Test|Tests|IT)\.java)$")


def is_test_path(path: str) -> bool:
    """Heuristic: a file under a test directory or named like a test.

    Used only to rank production code above test code, never to hide anything.
    """
    *dirs, name = path.split("/")
    return any(d in _TEST_DIRS for d in dirs) or bool(_TEST_FILE.match(name))


def kind_plural(kind: str) -> str:
    return "classes" if kind == "class" else kind + "s"


def kind_rank(kind: str) -> int:
    """Lower is shown first: types, then callables, then fields."""
    if kind in TYPE_KINDS:
        return 0
    if kind == "field":
        return 2
    return 1


def short_name(sym: SymbolRow) -> str:
    """Qualified name relative to the file's module: 'Order.add', 'Item.save'.

    The file path already says which module a symbol is in, so repeating the
    package in every line only costs tokens. Tools accept this form back.
    """
    prefix = sym.module + "." if sym.module else ""
    qn = sym.qualified_name
    return qn[len(prefix) :] if prefix and qn.startswith(prefix) else qn


def symbol_line(sym: SymbolRow) -> str:
    """'25 method Order.add  public Order add(Item item)' (the file is the group header)."""
    return f"{sym.start_line} {sym.kind} {short_name(sym)}  {clip(sym.signature, SIGNATURE_WIDTH)}"


def symbol_rank_key(sym: SymbolRow) -> tuple:
    """Production code first, then types before members, then location."""
    return (is_test_path(sym.path), kind_rank(sym.kind), sym.path, sym.start_line)
