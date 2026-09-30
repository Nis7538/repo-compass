"""search_symbols, get_symbol and file_outline as pure functions over the fixture index."""

from compass.tools.outline import file_outline
from compass.tools.symbols import get_symbol, search_symbols

MODEL = "java/shop/src/main/java/com/example/shop/model/"


def test_search_exact_matches_grouped_by_file_with_prefix(fx_conn):
    assert search_symbols(fx_conn, "save", None, 10).splitlines() == [
        '4 symbols match "save" (exact 4)',
        "paths under python/src/inventory/",
        "models.py",
        "  18 method Base.save  def save(self)",
        "  50 method Item.save  def save(self)",
        "services.py",
        "  27 method StockService.save  def save(self)",
        "utils.py",
        "  4 function save  def save(obj)",
    ]


def test_search_truncates_lowest_ranked_with_marker(fx_conn):
    lines = search_symbols(fx_conn, "total", None, 2).splitlines()
    assert lines[0] == '4 symbols match "total" (exact 3, word match 1)'
    assert lines[-1] == "[truncated: 2 more] limit=4 shows all"
    # Exact matches outrank the word match (subtotal), so they survive the cut.
    assert "subtotal" not in "\n".join(lines)


def test_search_word_match_and_kind_filter(fx_conn):
    assert search_symbols(fx_conn, "stock", "class", 10).splitlines() == [
        '1 symbol matches "stock" kind=class (exact 0, word match 1)',
        "python/src/inventory/services.py",
        "  12 class StockService  class StockService(Base)",
    ]


def test_search_rejects_unknown_kind_and_empty_query(fx_conn):
    assert search_symbols(fx_conn, "x", "func", 10).startswith('Unknown kind "func". Use one of:')
    assert search_symbols(fx_conn, "  ", None, 10).startswith("Empty query.")
    assert search_symbols(fx_conn, "zzzz", None, 10) == 'No symbols match "zzzz".'


def test_get_symbol_method_body_is_dedented(fx_conn):
    assert get_symbol(fx_conn, "Order.addAll", 60).splitlines() == [
        f"method Order.addAll @ {MODEL}Order.java:34-39",
        "public Order addAll(Item... items) {",
        "    for (Item item : items) {",
        "        add(item);",
        "    }",
        "    return this;",
        "}",
    ]


def test_get_symbol_long_body_points_at_the_rest(fx_conn):
    assert get_symbol(fx_conn, "Item.validate", 3).splitlines()[-1] == (
        "[truncated: 2 more lines] read python/src/inventory/models.py lines 58-59"
    )


def test_get_symbol_class_shows_members_not_body(fx_conn):
    assert get_symbol(fx_conn, "Order.Line", 10).splitlines() == [
        f"class Order.Line @ {MODEL}Order.java:62-74",
        "public static class Line",
        "doc: A single order line.",
        "  63 field private final Item item",
        "  64 field private final int quantity",
        "  66-69 constructor Line(Item item, int quantity)",
        "  71-73 method double subtotal()",
    ]


def test_get_symbol_python_class_with_decorator_and_doc(fx_conn):
    lines = get_symbol(fx_conn, "inventory.models.Item", 3).splitlines()
    assert lines[:4] == [
        "class Item @ python/src/inventory/models.py:27-66",
        "decorators: dataclass",
        "class Item(Base, metaclass=Meta)",
        "doc: A stock-keeping unit.",
    ]
    # Types rank first, so the nested class History survives a limit of 3.
    assert "  61-63 class History" in lines
    assert lines[-1] == "[truncated: 7 more (methods 6, functions 1)] limit=10 shows all"


def test_get_symbol_by_path_line_and_ambiguity(fx_conn):
    assert get_symbol(fx_conn, "model/Order.java:30", 60).startswith(
        f"method Order.add @ {MODEL}Order.java:29-32"
    )
    assert get_symbol(fx_conn, "Order.add", 60).startswith('"Order.add" matches 2 symbols.')


def test_get_symbol_notes_a_file_changed_since_indexing(copy_fixture, tmp_path):
    from compass.indexer.pipeline import index_repo
    from compass.store.db import open_index

    repo = copy_fixture("python")
    index_repo(repo, tmp_path / "i.db")
    utils = repo / "src" / "inventory" / "utils.py"
    utils.write_text(utils.read_text() + "\n# edited\n")
    conn = open_index(tmp_path / "i.db")
    try:
        text = get_symbol(conn, "inventory.utils.save", 60)
    finally:
        conn.close()
    assert "[file changed since it was indexed; line numbers may be off]" in text


def test_outline_ranks_types_then_callables_then_fields(fx_conn):
    assert file_outline(fx_conn, "model/Order.java", 10).splitlines() == [
        f"{MODEL}Order.java (java, 83 lines, module com.example.shop.model)",
        "  9-83 public class Order implements Priced",
        "    16-18 constructor public Order()",
        "    20-22 constructor public Order(int discount)",
        "    25-27 method public Order add(Item item)",
        "    29-32 method public Order add(Item item, int quantity)",
        "    34-39 method public Order addAll(Item... items)",
        "    41-43 method public <T extends Comparable<T>> T largest(List<T> values)",
        "    62-74 public static class Line",
        "    76-80 class Tracker",
        "    82 enum Channel",
        "[truncated: 13 more (fields 8, methods 4, constructors 1)] limit=23 shows all",
    ]


def test_outline_reports_parse_errors_and_unknown_files(fx_conn):
    lines = file_outline(fx_conn, "Broken.java", 10).splitlines()
    assert lines[1] == "parse errors: yes (tree-sitter recovered; some symbols may be missing)"
    assert file_outline(fx_conn, "Nope.java", 10).startswith('No indexed file matches "Nope.java"')
