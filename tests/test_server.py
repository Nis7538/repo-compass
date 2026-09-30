"""The MCP server through an in-process client.

mode="legacy" runs the real initialize handshake and JSON-RPC serialization over
memory streams, i.e. what a stdio client sees minus the pipe. Every tool is
called through the protocol and checked against the pure function it wraps.
"""

import json
import threading

import pytest
from mcp import Client

from compass.index_manager import IndexManager
from compass.server import INSTRUCTIONS, build_server
from compass.store.db import open_index
from compass.tools.caps import TOOLS_LIST_CAP
from compass.tools.deps import module_dependencies
from compass.tools.outline import file_outline
from compass.tools.references import find_references
from compass.tools.render import estimate_tokens
from compass.tools.symbols import get_symbol, search_symbols
from tests.helpers import FIXTURES

TOOLS = {
    "repo_summary",
    "search_symbols",
    "get_symbol",
    "find_references",
    "file_outline",
    "module_dependencies",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def call(client: Client, name: str, **arguments) -> str:
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    # Text only: a structured copy of the same answer would double its token cost.
    assert result.structured_content is None
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    return result.content[0].text


@pytest.fixture
async def client(tmp_path):
    async with Client(build_server(FIXTURES, tmp_path / "i.db"), mode="legacy") as c:
        yield c


@pytest.mark.anyio
async def test_lists_six_read_only_text_tools_within_budget(client):
    listed = (await client.list_tools()).tools
    assert {t.name for t in listed} == TOOLS
    for t in listed:
        assert t.annotations.read_only_hint is True
        assert t.output_schema is None
    size = sum(
        estimate_tokens(t.name + (t.description or "") + json.dumps(t.input_schema))
        for t in listed
    )
    assert size <= TOOLS_LIST_CAP, size
    assert estimate_tokens(INSTRUCTIONS) <= 200


@pytest.mark.anyio
async def test_each_tool_returns_its_pure_function_output(client, tmp_path):
    cases = [
        ("search_symbols", {"query": "save"}, lambda c: search_symbols(c, "save", None, 10)),
        ("get_symbol", {"symbol": "Order.addAll"}, lambda c: get_symbol(c, "Order.addAll", 60)),
        (
            "find_references",
            {"symbol": "Order.java:25"},
            lambda c: find_references(c, "Order.java:25", 10),
        ),
        ("file_outline", {"path": "Order.java"}, lambda c: file_outline(c, "Order.java", 10)),
        (
            "module_dependencies",
            {"module": "inventory.models"},
            lambda c: module_dependencies(c, "inventory.models", 10),
        ),
        (
            "module_dependencies",
            {"include_tests": True},
            lambda c: module_dependencies(c, None, 10, include_tests=True),
        ),
    ]
    for name, arguments, direct in cases:
        text = await call(client, name, **arguments)
        conn = open_index(tmp_path / "i.db")
        try:
            assert text == direct(conn), name
        finally:
            conn.close()


@pytest.mark.anyio
async def test_repo_summary_reports_fresh_index(client):
    text = await call(client, "repo_summary")
    assert text.splitlines()[0].endswith("/fixtures  index: fresh")


@pytest.mark.anyio
async def test_limits_are_clamped_not_rejected_and_the_answer_says_so(client):
    low = (await call(client, "search_symbols", query="save", limit=0)).splitlines()
    assert low[0] == "[limit=0 clamped to 1, the minimum]"
    assert low[-1] == "[truncated: 3 more] limit=4 shows all"
    high = await call(client, "search_symbols", query="save", limit=10_000)
    assert high.splitlines()[0] == "[limit=10000 clamped to 200, the maximum]"
    assert "truncated" not in high
    body = await call(client, "get_symbol", symbol="Order.addAll", max_lines=-5)
    assert body.startswith("[max_lines=-5 clamped to 1, the minimum]\nmethod Order.addAll")


@pytest.mark.anyio
async def test_in_range_limits_add_no_note(client):
    assert (await call(client, "search_symbols", query="save", limit=200)).startswith("4 symbols")
    assert (await call(client, "search_symbols", query="save", limit=1)).startswith("4 symbols")


def test_clamp_note_fits_its_reserved_width():
    from compass.server import bounded
    from compass.tools.render import NOTE_WIDTH

    value, note = bounded("max_lines", 10**40, 400)
    assert value == 400
    assert len(note) <= NOTE_WIDTH


@pytest.mark.anyio
async def test_error_answers_are_plain_text_not_protocol_errors(client):
    assert (await call(client, "get_symbol", symbol="Nope.nope")).startswith(
        'No symbol "Nope.nope"'
    )
    assert (await call(client, "find_references", symbol="save")).startswith('"save" matches 4')
    assert (await call(client, "file_outline", path="Nope.java")).startswith("No indexed file")
    assert (await call(client, "search_symbols", query="x", kind="bogus")).startswith(
        'Unknown kind "bogus"'
    )


@pytest.mark.anyio
async def test_edits_and_deletions_are_seen_on_the_next_call(copy_fixture, tmp_path):
    repo = copy_fixture("python")
    async with Client(build_server(repo, tmp_path / "e.db"), mode="legacy") as c:
        assert "def save(obj):" in await call(c, "get_symbol", symbol="inventory.utils.save")
        utils = repo / "src" / "inventory" / "utils.py"
        utils.write_text("def save(obj, force=False):\n    return force\n")
        body = await call(c, "get_symbol", symbol="inventory.utils.save")
        assert body.splitlines()[1:] == ["def save(obj, force=False):", "    return force"]
        utils.unlink()
        assert (await call(c, "file_outline", path="inventory/utils.py")).startswith(
            'No indexed file matches "inventory/utils.py"'
        )


@pytest.mark.anyio
async def test_first_call_during_a_slow_first_build_says_retry(tmp_path):
    release = threading.Event()

    def slow_index(root, db):
        release.wait(10)

    manager = IndexManager(FIXTURES, tmp_path / "s.db", slow_index, cold_budget=0.05)
    try:
        async with Client(build_server(FIXTURES, tmp_path / "s.db", manager), mode="legacy") as c:
            text = await call(c, "search_symbols", query="save")
        assert text.startswith("[index: building, ")
        assert "Retry this call in a few seconds." in text
    finally:
        release.set()
