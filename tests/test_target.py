"""Resolving the `symbol` and `path` arguments agents pass to tools."""

from compass.indexer.pipeline import index_repo
from compass.store.db import open_index
from compass.tools.target import normalize_path, resolve_file, resolve_symbol
from tests.helpers import FIXTURES

ORDER = "java/shop/src/main/java/com/example/shop/model/Order.java"


def _qn(target):
    assert target.symbol is not None, target.message
    return target.symbol.qualified_name


def test_qualified_suffix_and_full_name(fx_conn):
    assert (
        _qn(resolve_symbol(fx_conn, "Order.addAll", 1000)) == "com.example.shop.model.Order.addAll"
    )
    assert _qn(resolve_symbol(fx_conn, "inventory.models.Item", 1000)) == "inventory.models.Item"


def test_class_wins_over_its_own_constructors(fx_conn):
    target = resolve_symbol(fx_conn, "Order", 1000)
    assert target.symbol.kind == "class"


def test_overloads_are_ambiguous_with_candidates(fx_conn):
    target = resolve_symbol(fx_conn, "Order.add", 1000)
    assert target.symbol is None
    assert target.message.splitlines() == [
        '"Order.add" matches 2 symbols. Pass one as path:line or a longer name:',
        ORDER,
        "  25 method Order.add  public Order add(Item item)",
        "  29 method Order.add  public Order add(Item item, int quantity)",
    ]


def test_path_line_picks_innermost_symbol(fx_conn):
    assert _qn(resolve_symbol(fx_conn, "model/Order.java:30", 1000)).endswith("Order.add")
    assert resolve_symbol(fx_conn, "model/Order.java:30", 1000).symbol.param_count == 2
    # Line of the nested class header, then a line inside it.
    assert _qn(resolve_symbol(fx_conn, "Order.java:62", 1000)).endswith("Order.Line")
    assert _qn(resolve_symbol(fx_conn, "Order.java:67:5", 1000)).endswith("Order.Line.Line")


def test_path_line_outside_any_symbol(fx_conn):
    target = resolve_symbol(fx_conn, "model/Order.java:2", 1000)
    assert target.symbol is None
    assert "file_outline" in target.message


def test_unknown_symbol_suggests_closest(fx_conn):
    target = resolve_symbol(fx_conn, "Order.subtot", 1000)
    assert target.symbol is None
    assert target.message == (
        'No symbol "Order.subtot" in the index. Closest: Order.Line.subtotal '
        f"({ORDER}:71). search_symbols finds names by words."
    )
    assert resolve_symbol(fx_conn, "zzzz", 1000).message == (
        'No symbol "zzzz" in the index. search_symbols finds names by words.'
    )


def test_file_by_unique_suffix_absolute_and_backslashes(fx_conn):
    assert resolve_file(fx_conn, "model/Order.java").file.path == ORDER
    assert resolve_file(fx_conn, "model\\Order.java").file.path == ORDER
    assert resolve_file(fx_conn, str(FIXTURES / ORDER)).file.path == ORDER
    assert resolve_file(fx_conn, "./" + ORDER).file.path == ORDER


def test_file_suffix_must_align_to_a_path_segment(fx_conn):
    assert resolve_file(fx_conn, "rder.java").file is None


def test_ambiguous_file_lists_candidates(tmp_path):
    for pkg in ("a", "b"):
        (tmp_path / "repo" / pkg).mkdir(parents=True)
        (tmp_path / "repo" / pkg / "util.py").write_text("def f():\n    pass\n")
    index_repo(tmp_path / "repo", tmp_path / "i.db")
    conn = open_index(tmp_path / "i.db")
    try:
        target = resolve_file(conn, "util.py")
    finally:
        conn.close()
    assert target.file is None
    assert target.message.splitlines() == [
        '"util.py" matches 2 files; pass more of the path:',
        "a/util.py",
        "b/util.py",
    ]


def test_missing_file_says_what_is_indexed(fx_conn):
    message = resolve_file(fx_conn, "src/Nope.java").message
    assert message.startswith('No indexed file matches "src/Nope.java".')
    assert "Only .java and .py files are indexed" in message


def test_normalize_path(fx_conn):
    assert normalize_path(fx_conn, " .\\a\\b.py ") == "a/b.py"
