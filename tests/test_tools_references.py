"""find_references as a pure function over the fixture index."""

from compass.tools.references import find_references

SHOP = "java/shop/src/main/java/com/example/shop/"


def test_references_grouped_ranked_with_caller_and_context(fx_conn):
    assert find_references(fx_conn, "Order.java:25", 10).splitlines() == [
        f"method Order.add @ {SHOP}model/Order.java:25  public Order add(Item item)",
        "3 call sites in 2 files (exact 1, likely 2)",
        "rank: exact > likely > possible, non-test first, then path:line",
        f"paths under {SHOP}",
        "model/Order.java",
        "  36 exact Order.addAll: add(item);",
        # Known false positive (List.add), labeled likely: docs/adr/003 "No type inference".
        "  30 likely Order.add: lines.add(new Line(item, quantity));",
        "service/OrderService.java",
        "  17 likely OrderService.checkout: order.add(item);",
    ]


def test_references_truncation_marker_summarizes_what_was_cut(fx_conn):
    lines = find_references(fx_conn, "Order.Line", 1).splitlines()
    assert lines[1] == "2 call sites in 2 files (exact 2)"
    assert lines[-1] == (
        "[truncated: 1 more (exact 1; 1 file, most in OrderService.java 1)] limit=2 shows all"
    )


def test_references_python_class_constructor_calls(fx_conn):
    text = find_references(fx_conn, "inventory.models.Item", 10)
    assert "  18 exact StockService.add: item = It(sku)" in text  # aliased import


def test_references_none_found_explains_scope(fx_conn):
    lines = find_references(fx_conn, "Report.total", 10).splitlines()
    assert lines[1].startswith("No call sites found. Only calls and `new` are indexed")


def test_references_ambiguous_name_lists_candidates(fx_conn):
    assert find_references(fx_conn, "save", 10).startswith('"save" matches 4 symbols.')
