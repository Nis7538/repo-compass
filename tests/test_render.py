"""Layout and budget rules in tools/render.py and tools/rank.py."""

from compass.tools.rank import is_test_path, kind_rank
from compass.tools.render import (
    HEADROOM,
    Entry,
    clip,
    common_dir,
    enforce_cap,
    estimate_tokens,
    fit_grouped,
    fit_lines,
    truncated,
)


def test_estimate_rounds_up():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abc") == 1
    assert estimate_tokens("abcd") == 2


def test_clip_collapses_whitespace_and_marks_cut():
    assert clip("  a   b\n c ", 10) == "a b c"
    assert clip("abcdefghij", 5) == "abcd…"


def test_truncation_marker_format():
    assert truncated(30) == "[truncated: 30 more]"
    assert (
        truncated(3, "likely 2, possible 1", "limit=13 shows all")
        == "[truncated: 3 more (likely 2, possible 1)] limit=13 shows all"
    )


def test_common_dir_needs_two_paths_and_two_segments():
    assert common_dir(["a/b/c/X.java", "a/b/d/Y.java"]) == "a/b/"
    assert common_dir(["a/b/X.java", "a/b/X.java"]) == ""  # one distinct path
    assert common_dir(["a/X.java", "a/Y.java"]) == ""  # one shared segment
    assert common_dir(["a/b/X.java", "c/b/Y.java"]) == ""


def test_fit_grouped_groups_by_first_appearance_and_factors_prefix():
    entries = [
        Entry("src/p/B.java", "1 best"),
        Entry("src/p/A.java", "2 second"),
        Entry("src/p/B.java", "3 third"),
    ]
    lines, shown = fit_grouped(["head"], entries, 1000)
    assert shown == 3
    assert lines == [
        "head",
        "paths under src/p/",
        "B.java",
        "  1 best",
        "  3 third",
        "A.java",
        "  2 second",
    ]


def test_fit_grouped_keeps_ranked_prefix_under_cap():
    entries = [Entry(f"f{i % 3}.py", "x" * 50) for i in range(200)]
    cap = 300  # tokens
    lines, shown = fit_grouped([], entries, cap)
    assert 0 < shown < 200
    text = "\n".join(lines)
    assert len(text) + HEADROOM <= cap * 3


def test_fit_lines_reports_how_many_fitted():
    lines, shown = fit_lines(["h"], ["a" * 100] * 50, 400)
    assert lines[0] == "h"
    assert shown == len(lines) - 1 < 50


def test_enforce_cap_is_noop_under_cap_and_cuts_over_it():
    assert enforce_cap("short", 10) == "short"
    cut = enforce_cap("\n".join(["y" * 20] * 100), 50)
    assert len(cut) <= 150
    assert cut.endswith("[truncated: output cap reached]")


def test_test_path_heuristic():
    assert is_test_path("src/test/java/com/x/OrderTest.java")
    assert is_test_path("tests/test_models.py")
    assert is_test_path("pkg/models_test.py")
    assert is_test_path("src/main/java/com/x/OrderServiceIT.java")
    assert not is_test_path("src/main/java/com/x/Order.java")
    assert not is_test_path("src/inventory/testing_utils_not.py")
    assert not is_test_path("src/contest/Main.java")


def test_kind_rank_orders_types_callables_fields():
    assert kind_rank("class") < kind_rank("method") < kind_rank("field")
    assert kind_rank("record") == kind_rank("interface") == 0
