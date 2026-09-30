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


def test_list_a_package_types_first_with_counts_that_survive_the_cut(fx_conn):
    assert search_symbols(fx_conn, "", None, 4, path="model").splitlines() == [
        "39 symbols under model (5 files): class 3, interface 1, enum 2, record 1,"
        " annotation 1, method 12, constructor 5, field 14",
        "paths under java/shop/src/main/java/com/example/shop/model/",
        "Audited.java",
        "  6 annotation Audited  public @interface Audited",
        "Item.java",
        "  3 record Item  public record Item(String sku, double price)",
        "Order.java",
        "  9 class Order  public class Order implements Priced",
        "  62 class Order.Line  public static class Line",
        "[truncated: 35 more (fields 14, methods 12, constructors 5, enums 2, classes 1,"
        " interfaces 1)] limit=39 shows all",
    ]


def test_list_main_classes_of_a_package(fx_conn):
    # The M2 end-to-end question that had no answer: "main classes of package X".
    lines = search_symbols(fx_conn, "", "class", 10, path="com.example.shop.service")
    assert lines.splitlines() == [
        "2 symbols kind=class under com.example.shop.service (2 files)",
        "paths under java/shop/src/main/java/com/example/shop/service/",
        "BaseService.java",
        "  3 class BaseService  public abstract class BaseService",
        "OrderService.java",
        "  10 class OrderService  public class OrderService extends BaseService",
    ]


def test_path_scope_matches_whole_segments_directories_and_files(fx_conn):
    def header(path, query="save"):
        return search_symbols(fx_conn, query, None, 10, path=path).splitlines()[0]

    # A file by suffix, with or without its extension, or by module name.
    for path in ("inventory/models.py", "inventory/models", "inventory.models", "models.py"):
        assert header(path) == f'2 symbols match "save" under {path} (exact 2)', path
    # A directory, with or without the trailing slash, and the whole path.
    assert header("src/inventory/") == '4 symbols match "save" under src/inventory (exact 4)'
    assert header("python/src/inventory/utils.py").startswith("1 symbol matches")
    # Segments are whole: "odels" is not "models", "inv_ntory" is not a LIKE pattern.
    for path in ("odels", "odels.py", "inv_ntory", "inventory/model"):
        assert header(path).startswith(f'No indexed file under "{path}"'), path


def test_trailing_slash_reads_a_word_as_a_directory(fx_conn):
    # "scripts" is a directory but no module has that name (scripts/seed.py is "seed").
    assert search_symbols(fx_conn, "", None, 10, path="scripts").startswith(
        'No indexed file under "scripts".'
    )
    assert search_symbols(fx_conn, "", None, 10, path="scripts/") == (
        "No symbols under scripts (1 file)."
    )


def test_unknown_scope_suggests_close_names(fx_conn):
    assert search_symbols(fx_conn, "", None, 10, path="modle") == (
        'No indexed file under "modle". Similar names: model. Pass a directory ("model/"),'
        ' a file ("Order.java") or a package or module ("com.x.model", "inventory.models").'
    )


def test_query_with_scope_keeps_word_matches_inside_it(fx_conn):
    assert search_symbols(fx_conn, "total", None, 10, path="service").splitlines() == [
        '1 symbol matches "total" under service (exact 0, word match 1)',
        "java/shop/src/main/java/com/example/shop/service/OrderService.java",
        "  33 method OrderService.totals  public List<Double> totals(List<Order> orders)",
    ]


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
