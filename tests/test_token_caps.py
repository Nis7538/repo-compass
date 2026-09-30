"""Every tool response stays under its documented token cap, even on a hostile repo.

The stress repo (tests/stress_corpus.py) has a method with 500+ callers, a class
with 300 members, a 400-line method, a 1,000-char source line, deep paths and a
60-package import cycle. Each tool is called through the MCP client at its
default limit and at the maximum, with a worst-case index status line
prepended, and the response must fit its cap and say what it cut.
"""

import re
from pathlib import Path

import pytest
from mcp import Client

from compass.index_manager import Freshness
from compass.indexer.pipeline import index_repo
from compass.server import build_server
from compass.tools.caps import CAPS, MAX_BODY_LINES, MAX_LIMIT, STATUS_CAP
from compass.tools.render import STATUS_WIDTH, estimate_tokens
from tests.stress_corpus import write_stress_repo

DOCS = Path(__file__).resolve().parent.parent / "docs" / "tools.md"
STATUS = "[index: refresh running 99s; " + "x" * (STATUS_WIDTH - 31) + "]"


class StaleManager:
    """Always 'ready, but refreshing': the longest status line the server can add."""

    def start(self):
        pass

    def ensure_fresh(self):
        return Freshness(True, STATUS, "refresh running 99s")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def stress_db(tmp_path_factory):
    root = write_stress_repo(tmp_path_factory.mktemp("stress") / "repo")
    db = root.parent / "index.db"
    index_repo(root, db)
    return root, db


# (tool, arguments, too big to show whole even at the maximum limit)
CALLS = [
    ("repo_summary", {}, True),
    ("search_symbols", {"query": "number"}, True),
    ("search_symbols", {"query": "process"}, False),
    ("get_symbol", {"symbol": "Big.huge"}, True),
    ("get_symbol", {"symbol": "org.example.deeply.nested.core.Big"}, True),
    ("find_references", {"symbol": "Hub.process"}, True),
    ("file_outline", {"path": "core/Big.java"}, True),
    ("module_dependencies", {}, True),
    ("module_dependencies", {"module": "org.example.deeply.nested.level00"}, False),
]


def _with_max(tool: str, arguments: dict) -> dict:
    key = "max_lines" if tool == "get_symbol" else "limit"
    return {**arguments, key: MAX_BODY_LINES if tool == "get_symbol" else MAX_LIMIT}


@pytest.mark.anyio
@pytest.mark.parametrize("at_max", [False, True], ids=["default-limit", "max-limit"])
async def test_every_response_fits_its_cap(stress_db, at_max):
    root, db = stress_db
    async with Client(build_server(root, db, StaleManager()), mode="legacy") as client:
        for tool, arguments, too_big in CALLS:
            if at_max:
                arguments = _with_max(tool, arguments)
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, (tool, result.content)
            text = result.content[0].text
            tokens = estimate_tokens(text)
            assert tokens <= CAPS[tool], f"{tool} {arguments}: {tokens} > {CAPS[tool]}"
            assert text.startswith(STATUS)
            if too_big:
                assert "[truncated: " in text, f"{tool} {arguments}: cut without a marker"


@pytest.mark.anyio
async def test_error_and_status_only_answers_fit_the_status_cap(stress_db):
    root, db = stress_db
    async with Client(build_server(root, db, StaleManager()), mode="legacy") as client:
        for tool, arguments in [
            ("get_symbol", {"symbol": "no.such.Thing"}),
            ("file_outline", {"path": "Nope.java"}),
            ("find_references", {"symbol": "zzz"}),
        ]:
            text = (await client.call_tool(tool, arguments)).content[0].text
            assert estimate_tokens(text) <= STATUS_CAP, (tool, text)


def test_documented_caps_match_enforced_caps():
    rows = re.findall(r"^\| `(\w+)` \| ([\d,]+) \|", DOCS.read_text(encoding="utf-8"), re.M)
    documented = {tool: int(cap.replace(",", "")) for tool, cap in rows}
    assert documented == CAPS
