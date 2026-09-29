"""`compass index` and `compass symbol` through typer's CliRunner."""

import pytest
from typer.testing import CliRunner

from compass.cli import app
from tests.helpers import FIXTURES

runner = CliRunner()


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    db = tmp_path_factory.mktemp("cli") / "index.db"
    result = runner.invoke(app, ["index", str(FIXTURES), "--db", str(db)])
    assert result.exit_code == 0, result.output
    return db


def _symbol(db, *args):
    return runner.invoke(app, ["symbol", *args, "--db", str(db)])


def test_index_reports_counts(tmp_path):
    db = tmp_path / "i.db"
    first = runner.invoke(app, ["index", str(FIXTURES / "java"), "--db", str(db)])
    assert first.exit_code == 0, first.output
    assert "files: 10 found, 10 parsed, 0 unchanged" in first.output
    again = runner.invoke(app, ["index", str(FIXTURES / "java"), "--db", str(db)])
    assert "10 found, 0 parsed, 10 unchanged" in again.output


def test_index_rejects_missing_directory(tmp_path):
    result = runner.invoke(app, ["index", str(tmp_path / "nope"), "--db", str(tmp_path / "i")])
    assert result.exit_code != 0


def test_symbol_lists_matches_with_location(db):
    result = _symbol(db, "Order.add")
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines == [
        "method com.example.shop.model.Order.add  public Order add(Item item)  "
        "java/shop/src/main/java/com/example/shop/model/Order.java:25",
        "method com.example.shop.model.Order.add  public Order add(Item item, int quantity)  "
        "java/shop/src/main/java/com/example/shop/model/Order.java:29",
    ]


def test_symbol_output_is_capped_with_truncation_marker(db):
    result = _symbol(db, "save", "--limit", "2")
    lines = result.output.splitlines()
    assert len(lines) == 3
    assert lines[-1] == "... truncated, 2 more"


def test_symbol_refs_show_tier_and_location(db):
    result = _symbol(db, "round", "--kind", "method", "--refs")
    assert result.exit_code == 0
    assert result.output.splitlines() == [
        "method com.example.shop.util.Money.round  public static double round(double value)  "
        "java/shop/src/main/java/com/example/shop/util/Money.java:6",
        "    exact    java/shop/src/main/java/com/example/shop/service/OrderService.java:30:16"
        "  round(...)",
        "method com.example.shop.util.Money.round  public static double round(double value, "
        "int places)  java/shop/src/main/java/com/example/shop/util/Money.java:10",
        "    exact    java/shop/src/main/java/com/example/shop/util/Money.java:7:16  round(...)",
    ]


def test_symbol_refs_are_capped(db):
    result = _symbol(db, "Order.Line", "--kind", "class", "--refs", "--ref-limit", "1")
    lines = result.output.splitlines()
    assert lines[1].startswith("    exact ")
    assert lines[2] == "    ... truncated, 1 more"


def test_symbol_without_callers_says_so(db):
    result = _symbol(db, "Report.total", "--refs")
    assert "(no call sites found)" in result.output


def test_symbol_no_match(db):
    assert "No symbols match 'zzz'" in _symbol(db, "zzz").output


def test_symbol_without_index_explains_what_to_do(tmp_path):
    result = runner.invoke(app, ["symbol", "x", "--db", str(tmp_path / "none.db")])
    assert result.exit_code == 1
    assert "Run: compass index" in result.output
