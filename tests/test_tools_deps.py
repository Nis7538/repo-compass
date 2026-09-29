"""module_dependencies over the fixture index, and cycle detection on small graphs."""

from collections import Counter

from compass.tools.deps import (
    Graph,
    _cycle_line,
    _loop_text,
    find_cycles,
    module_dependencies,
)


def test_overview_lists_most_imported_and_top_edges(fx_conn):
    lines = module_dependencies(fx_conn, None, 3).splitlines()
    assert lines == [
        "11 modules, 8 internal import edges, 0 cycles (edge weight = import statements;"
        " left out: 0 test files, 1 TYPE_CHECKING imports)",
        "most imported: inventory.models (by 3), inventory.helpers (by 2),"
        " com.example.shop.model (by 1), com.example.shop.util (by 1), inventory.utils (by 1)",
        "top edges:",
        "  inventory.services -> inventory.models 4",
        "  com.example.shop.service -> com.example.shop.model 2",
        "  seed -> inventory.models 2",
        "[truncated: 5 more edges] limit=8 shows all; or pass module=",
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


def test_type_checking_import_does_not_make_a_cycle(fx_conn):
    # models.py:9 imports services under `if TYPE_CHECKING:`; services imports models for real.
    assert module_dependencies(fx_conn, "inventory.models", 10).splitlines() == [
        "module inventory.models (1 file)",
        "imports 1 internal: inventory.helpers 1",
        "imported by 3: inventory.services 4, seed 2, inventory 1",
        "external 3: dataclasses 1, fastjson 1, typing 1",
        "cycles: none",
    ]


def test_runtime_cycle_names_the_import_behind_each_step(tmp_path):
    from compass.indexer.pipeline import index_repo
    from compass.store.db import open_index

    pkg = tmp_path / "repo" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("import os\nfrom pkg import b\n")
    (pkg / "b.py").write_text("from . import a\n")
    index_repo(tmp_path / "repo", tmp_path / "i.db")
    conn = open_index(tmp_path / "i.db")
    try:
        lines = module_dependencies(conn, "pkg.a", 10).splitlines()
    finally:
        conn.close()
    assert lines[-1] == "cycle: pkg.a -> pkg.b (a.py:2) -> pkg.a (b.py:1)"


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
        'No module "util" in the import graph (test files are left out). Similar:'
        " com.example.shop.util, inventory.utils. Call with no module for an overview."
    )


def test_test_files_are_left_out_of_the_graph(tmp_path):
    from compass.indexer.pipeline import index_repo
    from compass.store.db import open_index

    files = {
        "src/main/java/a/A.java": "package a;\nimport b.B;\npublic class A { B b; }\n",
        "src/main/java/b/B.java": "package b;\npublic class B {}\n",
        # Same package as B, as Java tests usually are; with it, a and b would form a cycle.
        "src/test/java/b/BTest.java": "package b;\nimport a.A;\nclass BTest { A a; }\n",
    }
    for path, text in files.items():
        (tmp_path / "repo" / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / path).write_text(text)
    index_repo(tmp_path / "repo", tmp_path / "i.db")
    conn = open_index(tmp_path / "i.db")
    try:
        text = module_dependencies(conn, None, 10)
    finally:
        conn.close()
    assert text.splitlines()[0] == (
        "2 modules, 1 internal import edges, 0 cycles"
        " (edge weight = import statements; left out: 1 test files)"
    )


def test_cycle_line_says_when_the_loop_is_one_example():
    graph = _graph(("a", "b"), ("b", "a"), ("b", "c"), ("c", "b"))
    assert _cycle_line(graph, {"a", "b", "c"}) == "cycle among 3 modules, e.g. a -> b -> a"
    assert _cycle_line(_graph(("a", "b"), ("b", "a")), {"a", "b"}) == "cycle: a -> b -> a"


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
