"""`compass index`, `compass symbol` and `compass review` through typer's CliRunner."""

import json
import sys

import pytest
from typer.testing import CliRunner

from compass import cli
from compass.cli import app
from tests.fake_anthropic import FakeClient, Text, ToolUse, reply
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


# --- compass review, with a scripted client in place of the Claude API ------------------


@pytest.fixture
def fake_api(monkeypatch):
    clients = []

    def make(script):
        def factory():
            client = FakeClient(list(script))
            clients.append(client)
            return client

        monkeypatch.setattr(cli, "make_client", factory)
        return clients

    monkeypatch.delenv("COMPASS_MODEL", raising=False)
    return make


def _review(shop, tmp_path, *args):
    repo, _ = shop
    return runner.invoke(
        app,
        [
            "review",
            "--repo",
            str(repo.root),
            "--log",
            str(tmp_path / "runs.jsonl"),
            "--transcript",
            str(tmp_path / "t.json"),
            "--db",
            str(tmp_path / "i.db"),
            *args,
        ],
    )


def test_review_prints_the_review_and_a_summary_line(shop, tmp_path, fake_api):
    clients = fake_api(
        [reply(ToolUse("t1", "list_files", {"glob": "*.py"})), reply(Text("## Summary\nok"))]
    )
    result = _review(shop, tmp_path, "--tools", "baseline", "--max-cost", "0.5")
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("## Summary\nok\n")
    assert "[baseline] 2 turns; tool calls: list_files 1;" in result.stderr
    assert "of $0.50; stop: end_turn" in result.stderr
    request = clients[0].requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["max_tokens"] <= 16000  # the default, cut to what $0.50 allows
    record = json.loads((tmp_path / "runs.jsonl").read_text(encoding="utf-8"))
    assert (record["tools_mode"], record["max_cost_usd"], record["max_turns"]) == (
        "baseline",
        0.5,
        20,
    )


def test_review_model_comes_from_the_environment(shop, tmp_path, fake_api, monkeypatch):
    clients = fake_api([reply(Text("review"))])
    monkeypatch.setenv("COMPASS_MODEL", "claude-sonnet-5-5")
    assert _review(shop, tmp_path, "--tools", "none").exit_code == 0
    assert clients[0].requests[0]["model"] == "claude-sonnet-5-5"
    _review(shop, tmp_path, "--tools", "none", "--model", "claude-haiku-4-5")
    assert clients[1].requests[0]["model"] == "claude-haiku-4-5"


def test_review_writes_to_out_but_never_inside_the_repo(shop, tmp_path, fake_api):
    fake_api([reply(Text("review text"))])
    out = tmp_path / "review.md"
    assert _review(shop, tmp_path, "--tools", "none", "--out", str(out)).exit_code == 0
    assert out.read_text(encoding="utf-8") == "review text\n"
    inside = shop[0].root / "review.md"
    result = _review(shop, tmp_path, "--tools", "none", "--out", str(inside))
    assert result.exit_code == 2
    assert "Refusing to write the review inside the repository" in result.stderr
    assert not inside.exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--tools", "shell"], "choose one of none, baseline, compass, both"),
        (["--model", "claude-imaginary-9"], "No price for model 'claude-imaginary-9'"),
        (["--base", "nope"], 'Unknown ref "nope"'),
        (["--max-tokens", "500"], "--max-tokens"),
    ],
)
def test_review_rejects_bad_options(shop, tmp_path, fake_api, args, message):
    fake_api([])
    result = _review(shop, tmp_path, *args)
    assert result.exit_code == 2
    assert message in result.output


def test_review_fails_when_the_api_fails(shop, tmp_path, fake_api):
    fake_api([ConnectionError("down")])
    result = _review(shop, tmp_path, "--tools", "none")
    assert result.exit_code == 1
    assert "error: ConnectionError: down" in result.stderr


def test_review_without_the_agent_extra_says_how_to_install_it(shop, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError
    result = _review(shop, tmp_path, "--tools", "none")
    assert result.exit_code == 2
    assert "uv sync --extra agent" in result.stderr
