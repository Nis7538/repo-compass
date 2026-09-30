"""hotspots on a scripted git history (tests/shop_history.py). Outputs are literal."""

import pytest

from compass.gitrepo import Commit, Touch
from compass.indexer.pipeline import index_repo
from compass.store.db import open_index
from compass.tools import hotspots as hotspots_module
from compass.tools.hotspots import count_churn, hotspots


@pytest.fixture
def spots(shop):
    _, db = shop
    conn = open_index(db)
    yield lambda since=None, limit=10, tests=False: hotspots(conn, since, limit, tests)
    conn.close()


SINCE_JANUARY = """\
hotspots since 2026-01-01: 7 commits (merges skipped), 9 of 9 indexed files changed; left out: 1 test file
score = commits x lines inside methods/functions, per file. Renames followed.
  114 = 6 x 19  shop/src/main/java/com/example/shop/model/Order.java  largest Order.total 7 lines, last 2026-03-02
  18 = 3 x 6  shop/src/main/java/com/example/shop/service/CartService.java  largest CartService.addToCart 3 lines, last 2026-03-02
  8 = 2 x 4  inventory/sync.py  largest sync 4 lines, last 2026-01-19
  6 = 2 x 3  inventory/stock.py  largest reserve 3 lines, last 2026-03-02
  6 = 1 x 6  shop/src/main/java/com/example/shop/job/ImportJob.java  largest ImportJob.run 6 lines, last 2026-01-05
  3 = 1 x 3  shop/src/main/java/com/example/shop/report/SalesReport.java  largest SalesReport.sum 3 lines, last 2026-01-05
  0 = 1 x 0  inventory/__init__.py  last 2026-01-05
  0 = 1 x 0  shop/src/main/java/com/example/shop/model/Item.java  last 2026-01-05"""  # noqa: E501 (literal tool output)


def test_since_a_date(spots):
    # Order.java: 6 commits (the merge is skipped). sync.py: 2, one made as sync_job.py.
    assert spots("2026-01-01") == SINCE_JANUARY
    assert spots("2026-01-01") == spots("2026-01-01")


def test_since_a_ref_counts_the_commits_after_it(spots):
    assert spots("main") == (
        "hotspots since main (2026-02-16): 1 commit (merges skipped), 4 of 9 indexed files"
        " changed; left out: 1 test file\n"
        "score = commits x lines inside methods/functions, per file. Renames followed.\n"
        "  19 = 1 x 19  shop/src/main/java/com/example/shop/model/Order.java  largest"
        " Order.total 7 lines, last 2026-03-02\n"
        "  6 = 1 x 6  shop/src/main/java/com/example/shop/service/CartService.java  largest"
        " CartService.addToCart 3 lines, last 2026-03-02\n"
        "  3 = 1 x 3  inventory/stock.py  largest reserve 3 lines, last 2026-03-02"
    )


def test_limit_cut_and_shared_prefix(spots):
    assert spots("2026-02-01", limit=2) == (
        "hotspots since 2026-02-01: 3 commits (merges skipped), 4 of 9 indexed files changed;"
        " left out: 1 test file\n"
        "score = commits x lines inside methods/functions, per file. Renames followed.\n"
        "paths under shop/src/main/java/com/example/shop/\n"
        "  38 = 2 x 19  model/Order.java  largest Order.total 7 lines, last 2026-03-02\n"
        "  12 = 2 x 6  service/CartService.java  largest CartService.addToCart 3 lines,"
        " last 2026-03-02\n"
        "[truncated: 1 more file] limit=3 shows all"
    )


def test_include_tests(spots):
    lines = spots("2026-01-01", tests=True).split("\n")
    assert lines[0].endswith("9 of 9 indexed files changed; tests included (1 file)")
    assert lines[4] == (
        "  16 = 2 x 8  shop/src/test/java/com/example/shop/model/OrderTest.java  largest"
        " OrderTest.addsOnce 4 lines, last 2026-03-02"
    )


def test_no_commits_in_the_window(spots):
    assert spots("2027-01-01") == (
        'No commits since "2027-01-01" (read as 2027-01-01). since takes a date git'
        ' understands ("6 months ago", "2026-01-01") or a ref ("v1.2").'
    )


def test_default_window_is_one_year(spots):
    # Relative to today, so only the wording is checked, not the dates.
    assert '"1 year ago"' in spots().split("\n")[0]


def test_commit_bound_is_stated(spots, monkeypatch):
    monkeypatch.setattr(hotspots_module, "MAX_COMMITS", 2)
    assert spots("2026-01-01").startswith(
        "hotspots since 2026-01-01: 2 commits (the most read; older ones not counted)"
    )


def test_shallow_clone_is_flagged(shop, tmp_path):
    repo, _ = shop
    clone = tmp_path / "clone"
    repo.git("clone", "-q", "--depth", "1", "--branch", "feature", repo.root.as_uri(), str(clone))
    index_repo(clone, tmp_path / "c.db")
    conn = open_index(tmp_path / "c.db")
    try:
        lines = hotspots(conn, "2026-01-01", 10, False).split("\n")
    finally:
        conn.close()
    assert lines[0].startswith("hotspots since 2026-01-01: 1 commit (merges skipped)")
    assert lines[1] == "note: shallow clone, commits before its cut are missing"


def test_renames_are_followed_back_through_a_chain():
    # Newest first: c.py was b.py, which was a.py. d.py was deleted and added again.
    commits = [
        Commit("5", 50, (Touch("M", "c.py"), Touch("A", "d.py"))),
        Commit("4", 40, (Touch("R", "c.py", "b.py"), Touch("D", "d.py"))),
        Commit("3", 30, (Touch("M", "b.py"), Touch("M", "d.py"))),
        Commit("2", 20, (Touch("R", "b.py", "a.py"),)),
        Commit("1", 10, (Touch("A", "a.py"), Touch("A", "d.py"))),
    ]
    churn, last = count_churn(commits)
    # d.py today is the file added in commit 5; commits 1-4 touched an earlier d.py.
    assert churn == {"c.py": 5, "d.py": 1}
    assert last == {"c.py": 50, "d.py": 50}
