"""The review agent's tool conditions: none, baseline, compass (in-process MCP) and both."""

import json

import pytest

from compass.agent.toolsets import (
    BASELINE_DEFINITIONS,
    BaselineTools,
    CompassTools,
    NoTools,
    open_toolset,
)
from compass.indexer.pipeline import index_repo
from compass.store.db import open_index
from compass.tools.baseline import read_file
from compass.tools.caps import BASELINE_TOOLS_LIST_CAP
from compass.tools.render import estimate_tokens
from compass.tools.symbols import search_symbols
from tests.gitrepo import ScriptedRepo
from tests.helpers import FIXTURES

COMPASS_TOOLS = {
    "repo_summary",
    "search_symbols",
    "get_symbol",
    "find_references",
    "file_outline",
    "module_dependencies",
    "diff_impact",
    "hotspots",
}
BASELINE_TOOLS = {"list_files", "read_file", "grep", "git_diff"}


@pytest.fixture
def repo(tmp_path):
    repo = ScriptedRepo(tmp_path / "repo")
    repo.write("app/orders.py", "def total(items):\n    return sum(items)\n")
    repo.base = repo.commit("one")
    repo.write("app/orders.py", "def total(items, tax=0):\n    return sum(items) + tax\n")
    repo.head_sha = repo.commit("two")
    return repo


@pytest.fixture
def compass_db(tmp_path):
    db = tmp_path / "fixtures.db"
    index_repo(FIXTURES, db)
    return db


def test_baseline_definitions_are_small_and_well_formed():
    size = sum(estimate_tokens(json.dumps(d)) for d in BASELINE_DEFINITIONS)
    assert size <= BASELINE_TOOLS_LIST_CAP, size
    assert {d["name"] for d in BASELINE_DEFINITIONS} == BASELINE_TOOLS
    for d in BASELINE_DEFINITIONS:
        assert set(d) == {"name", "description", "input_schema"}
        assert set(d["input_schema"]["required"]) <= set(d["input_schema"]["properties"])


def test_baseline_call_runs_the_pure_function(repo):
    tools = BaselineTools(repo.root, repo.base, repo.head_sha, "main...HEAD")
    text, is_error = tools.call("read_file", {"path": "app/orders.py"})
    assert not is_error
    assert text == read_file(repo.root.resolve(), "app/orders.py", 1, 200)
    diff, _ = tools.call("git_diff", {})
    assert diff.startswith("diff main...HEAD\n") and "+    return sum(items) + tax" in diff


def test_baseline_clamps_out_of_range_limits_and_says_so(repo):
    tools = BaselineTools(repo.root, repo.base, repo.head_sha, "x")
    text, _ = tools.call("list_files", {"limit": 10**9})
    assert text.splitlines()[0] == "[limit=1000000000 clamped to 500, the maximum]"
    text, _ = tools.call("git_diff", {"context": 99})
    assert text.splitlines()[0] == "[context=99 clamped to 10]"


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("read_file", {}, "missing required argument 'path'"),
        ("read_file", {"path": 3}, "'path' must be a str"),
        ("grep", {"pattern": "x", "limit": "ten"}, "'limit' must be an integer"),
        ("grep", {"pattern": "x", "limit": True}, "'limit' must be an integer"),
    ],
)
def test_baseline_bad_arguments_are_tool_errors(repo, name, arguments, message):
    tools = BaselineTools(repo.root, repo.base, repo.head_sha, "x")
    text, is_error = tools.call(name, arguments)
    assert is_error and message in text


def test_unknown_tools_are_errors(repo):
    assert NoTools().call("read_file", {})[1] is True
    tools = BaselineTools(repo.root, repo.base, repo.head_sha, "x")
    assert tools.call("write_file", {"path": "a"}) == ("Unknown tool write_file.", True)


def test_compass_tools_come_from_the_mcp_server(compass_db):
    tools = CompassTools(FIXTURES, compass_db)
    try:
        assert {d["name"] for d in tools.definitions()} == COMPASS_TOOLS
        for d in tools.definitions():
            assert d["description"] and d["input_schema"]["type"] == "object"
        text, is_error = tools.call("search_symbols", {"query": "save"})
        assert not is_error
        conn = open_index(compass_db)
        try:
            assert text == search_symbols(conn, "save", None, 10)
        finally:
            conn.close()
        assert tools.call("read_file", {"path": "x"}) == ("Unknown tool read_file.", True)
        text, is_error = tools.call("get_symbol", {})  # missing argument: MCP validation
        assert is_error and text
    finally:
        tools.close()


@pytest.mark.parametrize(
    ("mode", "names"),
    [
        ("none", set()),
        ("baseline", BASELINE_TOOLS),
        ("compass", COMPASS_TOOLS),
        ("both", COMPASS_TOOLS | BASELINE_TOOLS),
    ],
)
def test_each_mode_has_its_tools(mode, names, compass_db):
    with open_toolset(mode, FIXTURES, compass_db, "HEAD", "HEAD", "x") as tools:
        assert {d["name"] for d in tools.definitions()} == names
        if mode == "both":
            assert not tools.call("search_symbols", {"query": "save"})[1]
            assert "Order.java" in tools.call("list_files", {"glob": "*Order*"})[0]


def test_unknown_mode_is_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown tools mode"):
        open_toolset("everything", tmp_path, tmp_path / "db", "a", "b", "x")
