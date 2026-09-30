"""Symbol-level diff of two versions of some files (tools/changes.py). Pure: bytes in."""

from compass.tools.changes import Parsed, Version, diff_symbols, parse

ORDER = """package shop;

/** An order. */
public class Order {
    private int count;

    /** Adds one item. */
    public Order add(Item item) {
        count++;
        return this;
    }

    public Order add(Item item, int qty) {
        count += qty;
        return this;
    }

    public int total() {
        return count * 2;
    }

    public void untouched() {
        log("x");
    }

    static class Line {
        int qty() { return 1; }
    }
}
"""


def java(path: str, text: str) -> Parsed:
    return parse(Version(path, "java", "", text.encode()))


def py(path: str, module: str, text: str) -> Parsed:
    return parse(Version(path, "python", module, text.encode()))


def changes(base: list[Parsed], head: list[Parsed]) -> list[tuple]:
    return [
        (c.change, c.symbol.qualified_name, c.symbol.start_line, c.path)
        for c in diff_symbols(base, head)
    ]


def test_identical_files_have_no_changes():
    assert changes([java("Order.java", ORDER)], [java("Order.java", ORDER)]) == []


def test_body_change_marks_only_the_method():
    head = ORDER.replace("return count * 2;", "return count * 3;")
    assert changes([java("O.java", ORDER)], [java("O.java", head)]) == [
        ("body", "shop.Order.total", 18, "O.java")
    ]


def test_signature_change_keeps_the_old_declaration():
    head = ORDER.replace("public int total() {", "public long total() {")
    [change] = diff_symbols([java("O.java", ORDER)], [java("O.java", head)])
    assert change.change == "signature"
    assert change.was.signature == "public int total()"
    assert change.symbol.signature == "public long total()"
    assert change.parent.qualified_name == "shop.Order"


def test_whitespace_reindent_and_moves_inside_the_file_are_not_changes():
    reindented = ORDER.replace('        log("x");', '            log( "x" );')
    reindented = reindented.replace("        count++;", "\tcount++;")
    untouched = '    public void untouched() {\n        log("x");\n    }\n\n'
    nested = "    static class"
    moved = ORDER.replace(untouched, "").replace(nested, untouched + nested)
    assert changes([java("O.java", ORDER)], [java("O.java", reindented)]) == []
    assert changes([java("O.java", ORDER)], [java("O.java", moved)]) == []


def test_javadoc_edit_is_not_a_change_but_a_comment_in_a_body_is():
    doc = ORDER.replace("/** Adds one item. */", "/** Adds exactly one item. */")
    assert changes([java("O.java", ORDER)], [java("O.java", doc)]) == []
    comment = ORDER.replace("count++;", "count++; // one more")
    assert changes([java("O.java", ORDER)], [java("O.java", comment)]) == [
        ("body", "shop.Order.add", 8, "O.java")
    ]


def test_overloads_pair_by_signature_then_order():
    body = ORDER.replace("count += qty;", "count += qty * 2;")
    [change] = diff_symbols([java("O.java", ORDER)], [java("O.java", body)])
    assert (change.change, change.symbol.signature) == (
        "body",
        "public Order add(Item item, int qty)",
    )

    one_arg = "    /** Adds one item. */\n    public Order add(Item item) {\n"
    one_arg += "        count++;\n        return this;\n    }\n\n"
    removed = ORDER.replace(one_arg, "")
    [change] = diff_symbols([java("O.java", ORDER)], [java("O.java", removed)])
    assert (change.change, change.symbol.signature, change.symbol.start_line) == (
        "removed",
        "public Order add(Item item)",
        8,
    )


def test_nested_class_method_change_does_not_mark_the_outer_classes():
    head = ORDER.replace("return 1;", "return 2;")
    assert changes([java("O.java", ORDER)], [java("O.java", head)]) == [
        ("body", "shop.Order.Line.qty", 27, "O.java")
    ]


def test_added_and_removed_methods_and_annotations():
    head = ORDER.replace(
        "    public void untouched() {",
        "    public void extra() {}\n\n    @Deprecated\n    public void untouched() {",
    ).replace("    public int total() {\n        return count * 2;\n    }\n\n", "")
    assert changes([java("O.java", ORDER)], [java("O.java", head)]) == [
        ("added", "shop.Order.extra", 18, "O.java"),
        ("removed", "shop.Order.total", 18, "O.java"),  # line in base
        ("signature", "shop.Order.untouched", 20, "O.java"),
    ]


def test_class_moved_to_another_file_of_the_same_package_is_not_a_change():
    helper = "package shop;\n\nclass Rules {\n    int limit() { return 5; }\n}\n"
    base = [java("Order.java", ORDER + "\n" + helper.split("\n", 2)[2])]
    head = [java("Order.java", ORDER), java("Rules.java", helper)]
    assert changes(base, head) == []
    edited = helper.replace("return 5;", "return 6;")
    assert changes(base, [java("Order.java", ORDER), java("Rules.java", edited)]) == [
        ("body", "shop.Rules.limit", 4, "Rules.java")
    ]


INVENTORY = '''"""Inventory."""


class Item:
    kind = "item"

    def save(self):
        return 1

    def load(self):
        def inner():
            return 2

        return inner()


def helper():
    return 3
'''


def test_python_decorator_is_a_signature_change():
    head = INVENTORY.replace("    def save(self):", "    @cached\n    def save(self):")
    [change] = diff_symbols([py("m.py", "inv.m", INVENTORY)], [py("m.py", "inv.m", head)])
    assert (change.change, change.symbol.qualified_name) == ("signature", "inv.m.Item.save")
    assert change.symbol.decorators == ["cached"]


def test_python_class_attribute_is_the_class_body_and_nested_functions_stand_alone():
    head = INVENTORY.replace('kind = "item"', 'kind = "thing"').replace("return 2", "return 4")
    assert changes([py("m.py", "inv.m", INVENTORY)], [py("m.py", "inv.m", head)]) == [
        ("body", "inv.m.Item", 4, "m.py"),
        ("body", "inv.m.Item.load.inner", 11, "m.py"),
    ]


def test_python_function_moved_to_another_module_is_removed_and_added():
    # The qualified name follows the module, and importers of the old one break.
    base = [py("m.py", "inv.m", INVENTORY), py("n.py", "inv.n", "")]
    moved = INVENTORY.replace("\n\ndef helper():\n    return 3\n", "\n")
    head = [py("m.py", "inv.m", moved), py("n.py", "inv.n", "def helper():\n    return 3\n")]
    assert changes(base, head) == [
        ("removed", "inv.m.helper", 17, "m.py"),
        ("added", "inv.n.helper", 1, "n.py"),
    ]
