"""diff_impact on a scripted git history (tests/shop_history.py). Outputs are literal."""

import shutil

import pytest

from compass.indexer.pipeline import index_repo
from compass.store.db import open_index
from compass.tools.impact import clip_pair, diff_impact
from tests.gitrepo import ScriptedRepo
from tests.shop_history import JAVA, SALES_REPORT


@pytest.fixture
def impact(shop):
    _, db = shop
    conn = open_index(db)
    yield lambda base, head=None, limit=10: diff_impact(conn, base, head, limit)
    conn.close()


def _index(repo: ScriptedRepo, tmp_path, root=None):
    db = tmp_path / "own.db"
    index_repo(root or repo.root, db)
    return open_index(db)


FULL = """\
diff main...HEAD (merge base 6e9cdb5): 4 code files changed (1 test), 1 other file; 7 symbols changed (removed 2, signature 1, body 3, added 1)
modules touched 3: com.example.shop.model, com.example.shop.service, inventory.stock
inventory/stock.py
  -11 removed function release  dangling: 1 call (exact 1), 1 import
    <- sync (sync.py:9), import at sync.py:3
shop/src/main/java/com/example/shop/model/Order.java
  -14 removed method Order.addIfAbsent  dangling: 2 calls (likely 2), 0 imports
    <- ImportJob.run (ImportJob.java:10), OrderTest.addsOnce (OrderTest.java:6)
  9 signature method Order.add  public Order add(Item item, int qty)
    was: public Order add(Item item)
    callers 3, outside the diff 1 (likely 1)
    <- ImportJob.run (ImportJob.java:9)
  16 body method Order.total  callers 3, outside the diff 1 (likely 1)
    <- SalesReport.sum (SalesReport.java:7)
  24 added method Order.clear  public void clear()
shop/src/main/java/com/example/shop/service/CartService.java
  9 body method CartService.addToCart  callers 0
shop/src/test/java/com/example/shop/model/OrderTest.java
  9 body method OrderTest.totals  callers 0
importers of touched modules outside the diff: 3 files (0 tests): inventory/sync.py, shop/src/main/java/com/example/shop/job/ImportJob.java, shop/src/main/java/com/example/shop/report/SalesReport.java"""  # noqa: E501 (literal tool output)


def test_feature_branch_against_main(impact):
    assert impact("main", "HEAD") == FULL
    assert impact("main", "feature") == FULL.replace("main...HEAD", "main...feature")


def test_same_answer_every_time(impact):
    assert impact("main", "HEAD") == impact("main", "HEAD")


def test_limit_cuts_the_lowest_ranked_and_says_what_was_cut(impact):
    lines = impact("main", "HEAD", limit=3).split("\n")
    assert lines[-1] == (
        "[truncated: 4 more symbols (body 3, added 1; 1 with callers outside the diff)]"
        " limit=7 shows all"
    )
    assert lines[0] == FULL.split("\n")[0]  # totals do not shrink
    assert [line for line in lines if line.startswith("  ") and not line.startswith("    ")] == [
        "  -11 removed function release  dangling: 1 call (exact 1), 1 import",
        "  -14 removed method Order.addIfAbsent  dangling: 2 calls (likely 2), 0 imports",
        "  9 signature method Order.add  public Order add(Item item, int qty)",
    ]
    assert lines[-2].startswith("importers of touched modules outside the diff: 3 files")


def test_head_defaults_to_the_working_tree(shop, tmp_path, impact):
    # Nothing uncommitted: the working tree equals HEAD.
    assert impact("main") == FULL.replace("main...HEAD", "main...working tree")


def test_uncommitted_edit_moves_a_caller_inside_the_diff(shop, tmp_path):
    shutil.copytree(shop[0].root, tmp_path / "repo")
    repo = ScriptedRepo.existing(tmp_path / "repo")
    repo.write(JAVA + "report/SalesReport.java", SALES_REPORT.replace("total()", "total() * 2"))
    conn = _index(repo, tmp_path)
    try:
        text = diff_impact(conn, "main", None, 10)
    finally:
        conn.close()
    assert text.split("\n")[0].startswith(
        "diff main...working tree (merge base 6e9cdb5): 5 code files changed (1 test)"
    )
    assert "  16 body method Order.total  callers 3, outside the diff 0" in text.split("\n")
    assert "  6 body method SalesReport.sum  callers 0" in text.split("\n")
    assert "importers of touched modules outside the diff: 2 files (0 tests)" in text


def test_head_other_than_the_checkout_is_noted(impact):
    lines = impact("main~1", "main").split("\n")
    assert lines[0] == (
        "diff main~1...main (merge base fd0b728): 1 code file changed (0 test), 0 other files;"
        " 0 symbols changed"
    )
    assert lines[1] == "note: callers and importers are from the working tree, not from main"


def test_errors_are_short_and_actionable(impact, tmp_path):
    assert impact("mian") == (
        'Unknown ref "mian". Branches: main, feature, tweak. Also works: a tag, a commit, HEAD~3.'
    )
    assert impact("--output=x").startswith('Refusing ref "--output=x"')
    assert impact("main", "main") == "No changes between main and main."
    assert impact("main~1", "main~1") == "No changes between main~1 and main~1."


def test_not_a_git_repository(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    conn = _index(None, tmp_path, root=tmp_path)
    try:
        text = diff_impact(conn, "main", None, 10)
    finally:
        conn.close()
    assert text == (
        f"Not a git repository: {tmp_path.resolve().as_posix()}. diff_impact and hotspots"
        " need git; the other tools work without it."
    )


def test_unrelated_history_and_non_code_changes(tmp_path):
    repo = ScriptedRepo(tmp_path / "repo")
    repo.write("a.py", "def f():\n    return 1\n")
    repo.write("notes.md", "one\n")
    repo.commit("one")
    repo.write("notes.md", "two\n")
    repo.commit("two")
    repo.git("checkout", "-q", "--orphan", "other")
    repo.commit("unrelated")
    repo.checkout("main")
    conn = _index(repo, tmp_path)
    try:
        assert diff_impact(conn, "main~1", "main", 10) == (
            "No Java or Python files changed (1 other file)."
        )
        assert diff_impact(conn, "other", None, 10) == (
            "other and working tree share no history (no merge base)."
        )
    finally:
        conn.close()


def test_index_rooted_in_a_subdirectory_sees_only_its_files(shop, tmp_path):
    repo, _ = shop
    conn = _index(repo, tmp_path, root=repo.root / "shop")
    try:
        text = diff_impact(conn, "main", "HEAD", 10)
    finally:
        conn.close()
    assert text.split("\n")[0] == (
        "diff main...HEAD (merge base 6e9cdb5): 3 code files changed (1 test), 0 other files;"
        " 6 symbols changed (removed 1, signature 1, body 3, added 1)"
    )
    assert "src/main/java/com/example/shop/model/Order.java" in text.split("\n")
    assert "inventory" not in text


def test_class_moved_to_its_own_file_is_not_dangling(tmp_path):
    repo = ScriptedRepo(tmp_path / "repo")
    rules = "class Rules {\n    int limit() { return 5; }\n}\n"
    repo.write("p/Order.java", "package p;\n\npublic class Order {\n}\n\n" + rules)
    repo.write(
        "p/Cart.java",
        "package p;\n\nclass Cart {\n    int f() { return new Rules().limit(); }\n}\n",
    )
    repo.commit("one")
    repo.write("p/Order.java", "package p;\n\npublic class Order {\n}\n")
    repo.write("p/Rules.java", "package p;\n\n" + rules)
    repo.commit("two")
    conn = _index(repo, tmp_path)
    try:
        text = diff_impact(conn, "main~1", "main", 10)
    finally:
        conn.close()
    assert text == (
        "diff main~1...main (merge base 1253d72): 2 code files changed (0 test), 0 other files;"
        " 0 symbols changed\n"
        "modules touched 1: p\n"
        "no symbol changed: the edits are outside any class, method or function"
        " (imports, constants, module-level code)"
    )


def test_long_signatures_are_clipped_where_they_differ():
    params = ", ".join(f"Map<String, Integer> argumentNumber{k}" for k in range(6))
    old = f"public int m({params})"
    new = f"public int m({params}, int extra)"
    shown_new, shown_old = clip_pair(new, old, 120)
    assert shown_new.endswith("argumentNumber5, int extra)")
    assert shown_old.endswith("argumentNumber5)")
    assert shown_new.startswith("…") and len(shown_new) <= 120
    assert clip_pair("f(a)", "f(b)", 120) == ("f(a)", "f(b)")


def test_decorator_only_change_shows_the_decorators(tmp_path):
    repo = ScriptedRepo(tmp_path / "repo")
    repo.write("m.py", "def f():\n    return 1\n")
    repo.commit("one")
    repo.write("m.py", "@cache\ndef f():\n    return 1\n")
    repo.commit("two")
    conn = _index(repo, tmp_path)
    try:
        lines = diff_impact(conn, "main~1", "main", 10).split("\n")
    finally:
        conn.close()
    assert lines[3:5] == ["  1 signature function f  @cache def f()", "    was: def f()"]


def test_name_only_callers_are_counted_but_never_shown_as_examples(tmp_path):
    repo = ScriptedRepo(tmp_path / "repo")
    util = "package p;\n\nclass Util {\n    static int get(String s) { return 1; }\n}\n"
    repo.write("p/Util.java", util)
    repo.write(
        "p/Use.java",
        "package p;\n\nimport java.util.Map;\n\nclass Use {\n"
        '    Object f(Map<String, Object> m) { return m.get("k"); }\n}\n',
    )
    repo.commit("one")
    repo.write("p/Util.java", util.replace("return 1;", "return 2;"))
    repo.commit("two")
    conn = _index(repo, tmp_path)
    try:
        text = diff_impact(conn, "main~1", "main", 10)
    finally:
        conn.close()
    assert text.split("\n")[3:] == [
        "  4 body method Util.get  callers 1, outside the diff 1 (possible 1, name matches only)",
        "importers of touched modules outside the diff: none",
    ]
