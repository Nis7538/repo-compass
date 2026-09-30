"""MCP wiring only: tool names, descriptions and argument bounds.

The logic lives in compass.tools (pure functions over an index connection) and
compass.index_manager (freshness). Each tool call:
  1. asks the IndexManager for a fresh index (bounded wait, ADR-006);
  2. opens its own SQLite connection (tools run in worker threads);
  3. runs the tool function, then puts the index status line (if any) and a note
     about a clamped limit (if any) in front of the answer;
  4. applies the tool's hard token cap as a last guard (tools/caps.py).

Tools are registered with structured_output=False. In mcp 2.x a tool returning
str otherwise gets an output schema and the same text is sent a second time as
structuredContent, doubling the tokens the agent pays for.
"""

from collections.abc import Callable
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from compass import __version__
from compass.index_manager import Freshness, IndexManager
from compass.paths import default_db_path
from compass.store.db import open_index
from compass.tools.caps import CAPS, DEFAULT_BODY_LINES, DEFAULT_LIMIT, MAX_BODY_LINES, MAX_LIMIT
from compass.tools.deps import module_dependencies as deps_tool
from compass.tools.hotspots import hotspots as hotspots_tool
from compass.tools.impact import diff_impact as impact_tool
from compass.tools.outline import file_outline as outline_tool
from compass.tools.references import find_references as refs_tool
from compass.tools.render import NOTE_WIDTH, clip, enforce_cap
from compass.tools.summary import repo_summary as summary_tool
from compass.tools.symbols import get_symbol as get_symbol_tool
from compass.tools.symbols import search_symbols as search_tool

INSTRUCTIONS = (
    "Structural index of this repo's Java and Python code: symbols, call sites, imports. "
    "Answers are compact text, ranked best first and capped; a '[truncated: N more]' line "
    "says what was cut (raise limit, max 200). Symbols are named 'Class.method', a "
    "qualified name, or path:line. Paths may be any unique suffix. References are "
    "name-based (no type inference) and carry a confidence tier. diff_impact and "
    "hotspots also read git history. The index refreshes itself; an '[index: ...]' line "
    "appears only when it is building or may be stale."
)

READ_ONLY = ToolAnnotations(read_only_hint=True)


def bounded(name: str, value: int, high: int = MAX_LIMIT) -> tuple[int, str | None]:
    """Clamp a limit into 1..high, and say so when it had to.

    Out-of-range values are clamped, not rejected: the answer is still useful, and
    the caps bound the size either way. The note tells the agent it did not get
    what it asked for, so a short answer is not taken for a complete one.
    """
    clamped = max(1, min(value, high))
    if clamped == value:
        return value, None
    bound = "maximum" if clamped == high else "minimum"
    asked = clip(str(value), 12)  # an absurd value must not crowd out the useful part
    return clamped, clip(f"[{name}={asked} clamped to {clamped}, the {bound}]", NOTE_WIDTH)


def build_server(
    root: Path, db_path: Path | None = None, manager: IndexManager | None = None
) -> MCPServer:
    root = root.resolve()
    db_path = db_path or default_db_path(root)
    manager = manager or IndexManager(root, db_path)
    manager.start()
    server = MCPServer("repo-compass", version=__version__, instructions=INSTRUCTIONS)

    def run(tool: str, fn: Callable[..., str], note: str | None = None) -> str:
        freshness = manager.ensure_fresh()
        if not freshness.ready:
            return freshness.status or "[index: not ready]"
        conn = open_index(db_path)
        try:
            text = fn(conn, freshness)
        finally:
            conn.close()
        if note:
            text = note + "\n" + text
        if freshness.status:
            text = freshness.status + "\n" + text
        return enforce_cap(text, CAPS[tool])

    def tool(description: str):
        return server.tool(description=description, annotations=READ_ONLY, structured_output=False)

    @tool(
        "Overview: languages, size, packages by lines, index freshness. Start here."
        f" <= {CAPS['repo_summary']} tokens."
    )
    def repo_summary(limit: int = DEFAULT_LIMIT) -> str:
        n, note = bounded("limit", limit)

        def fn(conn, freshness: Freshness) -> str:
            return summary_tool(conn, freshness.summary, n)

        return run("repo_summary", fn, note)

    @tool(
        "Find symbols by name: 'save', 'Order.add', or words ('get user' finds"
        " get_user_by_id). Returns file:line, kind, signature; no bodies. Ranked exact name,"
        " then word match; non-test first. kind: class|interface|enum|record|annotation|"
        "method|constructor|function|field. path: only in this dir, file or package"
        " ('model/', 'com.x.model'); with query '' it lists what path defines, types first."
        f" <= {CAPS['search_symbols']} tokens."
    )
    def search_symbols(
        query: str, kind: str | None = None, path: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> str:
        n, note = bounded("limit", limit)
        return run("search_symbols", lambda conn, _: search_tool(conn, query, kind, n, path), note)

    @tool(
        "Source of one symbol: body for methods/functions, member list for types, plus doc."
        " symbol: 'Order.add', a qualified name, or path:line."
        f" <= {CAPS['get_symbol']} tokens."
    )
    def get_symbol(
        symbol: str,
        max_lines: int = DEFAULT_BODY_LINES,
    ) -> str:
        n, note = bounded("max_lines", max_lines, MAX_BODY_LINES)
        return run("get_symbol", lambda conn, _: get_symbol_tool(conn, symbol, n), note)

    @tool(
        "Call sites of a symbol, grouped by file, with calling symbol and source line."
        " Tier: exact (certain from syntax), likely (type visible, receiver type unknown),"
        " possible (name only). Ranked exact>likely>possible, non-test first."
        f" <= {CAPS['find_references']} tokens."
    )
    def find_references(symbol: str, limit: int = DEFAULT_LIMIT) -> str:
        n, note = bounded("limit", limit)
        return run("find_references", lambda conn, _: refs_tool(conn, symbol, n), note)

    @tool(
        "Symbols a file defines, with line ranges and signatures, without reading it."
        " Truncation keeps types, then methods, then fields."
        f" <= {CAPS['file_outline']} tokens."
    )
    def file_outline(path: str, limit: int = DEFAULT_LIMIT) -> str:
        n, note = bounded("limit", limit)
        return run("file_outline", lambda conn, _: outline_tool(conn, path, n), note)

    @tool(
        "Import graph between packages/modules, and import cycles. No module: overview."
        " module: a name, 'pkg.*' or a prefix: its imports, importers, external deps, cycles."
        " Test files are left out unless include_tests."
        f" <= {CAPS['module_dependencies']} tokens."
    )
    def module_dependencies(
        module: str | None = None, limit: int = DEFAULT_LIMIT, include_tests: bool = False
    ) -> str:
        n, note = bounded("limit", limit)
        return run(
            "module_dependencies",
            lambda conn, _: deps_tool(conn, module, n, include_tests),
            note,
        )

    @tool(
        "What a change touches: symbols changed between base and head (removed, signature,"
        " body, added), their callers outside the diff by tier, dangling uses of removed"
        " ones, importers. Compares the merge base, like a PR; head: a ref, default the"
        " working tree. Ranked: removed/signature changes with callers outside the diff"
        f" first. <= {CAPS['diff_impact']} tokens."
    )
    def diff_impact(base: str, head: str | None = None, limit: int = DEFAULT_LIMIT) -> str:
        n, note = bounded("limit", limit)
        return run("diff_impact", lambda conn, _: impact_tool(conn, base, head, n), note)

    @tool(
        "Files that change often and hold much code: commits x lines inside methods, per"
        " file, renames followed. since: a ref or a date ('6 months ago'), default 1 year."
        f" Test files left out unless include_tests. <= {CAPS['hotspots']} tokens."
    )
    def hotspots(
        since: str | None = None, limit: int = DEFAULT_LIMIT, include_tests: bool = False
    ) -> str:
        n, note = bounded("limit", limit)
        return run("hotspots", lambda conn, _: hotspots_tool(conn, since, n, include_tests), note)

    return server
