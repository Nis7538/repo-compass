"""`compass serve` as a real subprocess over stdio, the way Claude Code runs it.

Proves the stdio framing works and that nothing but protocol messages is
written to stdout (a stray print would corrupt the stream).
"""

import sys

import pytest
from mcp import Client, StdioServerParameters

from tests.helpers import FIXTURES


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.slow  # the subprocess imports the MCP SDK: several seconds on Windows
@pytest.mark.anyio
async def test_serve_over_stdio(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "compass.cli",
            "serve",
            "--repo",
            str(FIXTURES),
            "--db",
            str(tmp_path / "i.db"),
        ],
    )
    async with Client(params) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "find_references" in names
        result = await client.call_tool("find_references", {"symbol": "Money.java:6"})
    assert not result.is_error
    assert result.content[0].text.splitlines()[1] == "1 call site in 1 file (exact 1)"
