"""repo_summary as a pure function over the fixture index."""

from compass.tools.summary import _source_root, repo_summary


def test_summary_counts_and_packages_by_size(fx_conn):
    lines = repo_summary(fx_conn, "fresh", 10).splitlines()
    assert lines[0].endswith("/fixtures  index: fresh")
    assert lines[1:] == [
        "files 16 (java 10, python 6; tests 0)  lines 388  symbols 89  call sites 85"
        "  parse errors 1",
        "packages by lines (7 of 7):",
        "source roots (package dir = root + package path): java/shop/src/main/java/, python/src/",
        "  inventory  5 files 158 lines",
        "  com.example.shop.model  5 files 132 lines",
        "  com.example.shop.service  2 files 57 lines",
        "  com.example.shop.util  1 file 14 lines",
        "  com.example.shop.broken  1 file 11 lines",
        "  com.example.shop.other  1 file 10 lines",
        "  (no package)  python/scripts/  1 file 6 lines",
    ]


def test_summary_truncates_smallest_packages(fx_conn):
    lines = repo_summary(fx_conn, "fresh", 3).splitlines()
    assert lines[-2] == "  com.example.shop.service  2 files 57 lines"
    assert lines[-1] == "[truncated: 4 more] limit=7 shows all"


def test_source_root_requires_whole_path_segments():
    assert _source_root("src/main/java/com/x/", "com.x") == "src/main/java/"
    assert _source_root("com/x/", "com.x") == "./"
    assert _source_root("src/xcom/x/", "com.x") is None
    assert _source_root("scripts/", "") is None
