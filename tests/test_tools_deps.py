"""module_dependencies over the fixture index, and cycle detection on small graphs."""

from collections import Counter

from compass.tools.deps import Graph, _loop_text, find_cycles, module_dependencies


def test_overview_lists_cycles_most_imported_and_top_edges(fx_conn):
    lines = module_dependencies(fx_conn, None, 3).splitlines()
    assert lines == [
        "11 modules, 9 internal import edges, 1 cycle (edge weight = import statements)",
        "most imported: inventory.models (by 3), inventory.helpers (by 2),"
        " com.example.shop.model (by 1), com.example.shop.util (by 1), inventory.services (by 1)",
        "cycle, 2 modules: inventory.models -> inventory.services -> inventory.models",
        "top edges:",
        "  inventory.services -> inventory.models 4",
        "  com.example.shop.service -> com.example.shop.model 2",
        "  seed -> inventory.models 2",
        "[truncated: 6 more edges] limit=9 shows all; or pass module=",
    ]


def test_java_package_with_static_import_edge(fx_conn):
    assert module_dependencies(fx_conn, "com.example.shop.service", 10).splitlines() == [
        "module com.example.shop.service (2 files)",
        # util comes from `import static com.example.shop.util.Money.round`.
        "imports 2 internal: com.example.shop.model 2, com.example.shop.util 1",
        "imported by 0",
        "external 1: java.util 2",
        "cycles: none",
    ]


def test_python_module_in_a_cycle(fx_conn):
    assert module_dependencies(fx_conn, "inventory.models", 10).splitlines() == [
        "module inventory.models (1 file)",
        "imports 2 internal: inventory.helpers 1, inventory.services 1",
        "imported by 3: inventory.services 4, seed 2, inventory 1",
        "external 3: dataclasses 1, fastjson 1, typing 1",
        "cycle, 2 modules: inventory.models -> inventory.services -> inventory.models",
    ]


def test_package_group_hides_edges_inside_the_group(fx_conn):
    lines = module_dependencies(fx_conn, "inventory.*", 10).splitlines()
    assert lines[:3] == [
        "module inventory.* (5 modules, 5 files)",
        "imports 0 internal",
        "imported by 1: seed 2",
    ]
    # A prefix that is not itself a module groups automatically.
    assert module_dependencies(fx_conn, "com.example.shop", 10).startswith(
        "module com.example.shop.* (5 modules, 10 files)"
    )


def test_inline_lists_are_truncated_with_marker(fx_conn):
    line = module_dependencies(fx_conn, "inventory.models", 1).splitlines()[2]
    assert line == "imported by 3: inventory.services 4 [truncated: 2 more] limit=3"


def test_unknown_module_suggests_similar(fx_conn):
    assert module_dependencies(fx_conn, "util", 10) == (
        'No module "util" in the index. Similar: com.example.shop.util, inventory.utils.'
        " Call with no module for an overview."
    )


def _graph(*edges):
    modules = {m: 1 for edge in edges for m in edge}
    return Graph(modules, Counter({edge: 1 for edge in edges}))


def test_find_cycles_largest_first_and_ignores_dags():
    graph = _graph(
        ("a", "b"),
        ("b", "c"),
        ("c", "a"),  # 3-cycle
        ("x", "y"),
        ("y", "x"),  # 2-cycle
        ("c", "x"),  # links the cycles one way only
        ("p", "q"),  # DAG
    )
    assert find_cycles(graph) == [{"a", "b", "c"}, {"x", "y"}]
    assert _loop_text(graph, {"a", "b", "c"}) == "a -> b -> c -> a"
    assert find_cycles(_graph(("p", "q"), ("q", "r"))) == []


def test_find_cycles_handles_long_chains_without_recursion():
    chain = [(f"m{i:05d}", f"m{i + 1:05d}") for i in range(5000)]
    assert find_cycles(_graph(*chain, ("m05000", "m00000"))) == [
        {f"m{i:05d}" for i in range(5001)}
    ]
