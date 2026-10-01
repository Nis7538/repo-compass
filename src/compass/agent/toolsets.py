"""The four tool conditions the review agent can run with (M5 compares them).

- none: no tools; the agent has the diff in its prompt and nothing else.
- baseline: list_files, read_file, grep, git_diff (tools/baseline.py).
- compass: the eight compass MCP tools, from the real MCP server.
- both: baseline and compass together.

The compass tools are not re-declared here. CompassTools starts the server from
server.py and talks to it through the MCP SDK's in-process client (mode="legacy":
the real initialize handshake and JSON-RPC, minus the pipe), held open in an anyio
blocking portal so the synchronous loop can call it. The definitions and answers the
agent gets are therefore exactly what Claude Code gets, and cannot drift from them.

BaselineTools applies the same discipline as the server: out-of-range limits are
clamped with a note, and every answer goes through enforce_cap.

Every ToolSet is read-only and returns (text, is_error); none raises.
"""

from contextlib import AbstractContextManager, ExitStack
from pathlib import Path

from anyio.from_thread import start_blocking_portal

from compass.tools import baseline
from compass.tools.caps import (
    BASELINE_CAPS,
    DEFAULT_FILES_LIMIT,
    DEFAULT_GREP_LIMIT,
    DEFAULT_READ_LINES,
    MAX_DIFF_CONTEXT,
    MAX_FILES_LIMIT,
    MAX_LIMIT,
    MAX_READ_LINES,
)
from compass.tools.render import bounded, clip, enforce_cap

MODES = ("none", "baseline", "compass", "both")


class NoTools:
    def definitions(self) -> list[dict]:
        return []

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        return f"Unknown tool {clip(name, 60)}: no tools are available.", True

    def close(self) -> None:
        pass


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


STRING = {"type": "string"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}

BASELINE_DEFINITIONS = [
    {
        "name": "list_files",
        "description": (
            "Files in the repo (tracked, plus untracked and not ignored), grouped by"
            " directory. glob matches the whole path and * crosses /: '*.py', 'src/*';"
            f" ending in / means a directory. <= {BASELINE_CAPS['list_files']} tokens."
        ),
        "input_schema": _schema(
            {"glob": STRING, "limit": {**INTEGER, "default": DEFAULT_FILES_LIMIT}}, []
        ),
    },
    {
        "name": "read_file",
        "description": (
            "Numbered lines of one file, path relative to the repo root. Reads max_lines"
            f" (max {MAX_READ_LINES}) from start_line; a '[truncated: ...] start_line=N"
            f" continues' line says how to read on. <= {BASELINE_CAPS['read_file']} tokens."
        ),
        "input_schema": _schema(
            {
                "path": STRING,
                "start_line": {**INTEGER, "default": 1},
                "max_lines": {**INTEGER, "default": DEFAULT_READ_LINES},
            },
            ["path"],
        ),
    },
    {
        "name": "grep",
        "description": (
            "Lines matching a regex (git grep -E; fixed=true for plain text) in the repo's"
            " text files, grouped by file, in path order. path: only in this file or dir."
            f" <= {BASELINE_CAPS['grep']} tokens."
        ),
        "input_schema": _schema(
            {
                "pattern": STRING,
                "path": STRING,
                "fixed": {**BOOLEAN, "default": False},
                "limit": {**INTEGER, "default": DEFAULT_GREP_LIMIT},
            },
            ["pattern"],
        ),
    },
    {
        "name": "git_diff",
        "description": (
            "The diff under review (merge base to head), whole or for one path. context:"
            f" lines around each change (max {MAX_DIFF_CONTEXT})."
            f" <= {BASELINE_CAPS['git_diff']} tokens."
        ),
        "input_schema": _schema({"path": STRING, "context": {**INTEGER, "default": 3}}, []),
    },
]


class ArgumentError(Exception):
    pass


def _arg(arguments: dict, name: str, kind: type, default=None, required: bool = False):
    value = arguments.get(name, default)
    if value is None:
        if required:
            raise ArgumentError(f"missing required argument '{name}'")
        return None
    # bool is an int in Python; a limit of True is still a mistake.
    if kind is int and (isinstance(value, bool) or not isinstance(value, int)):
        raise ArgumentError(f"'{name}' must be an integer")
    if not isinstance(value, kind):
        raise ArgumentError(f"'{name}' must be a {kind.__name__}")
    return value


class BaselineTools:
    """list_files, read_file, grep and git_diff over one repository.

    base and head are the commits the review compares (the merge base and the head);
    label names them in git_diff's header ('main...HEAD').
    """

    def __init__(self, root: Path, base: str, head: str, label: str):
        self.root = root.resolve()
        self.base = base
        self.head = head
        self.label = label

    def definitions(self) -> list[dict]:
        return BASELINE_DEFINITIONS

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        handler = {
            "list_files": self._list_files,
            "read_file": self._read_file,
            "grep": self._grep,
            "git_diff": self._git_diff,
        }.get(name)
        if handler is None:
            return f"Unknown tool {clip(name, 60)}.", True
        try:
            text, note = handler(arguments)
        except ArgumentError as exc:
            return f"Invalid arguments for {name}: {exc}.", True
        if note:
            text = note + "\n" + text
        return enforce_cap(text, BASELINE_CAPS[name]), False

    def _list_files(self, args: dict) -> tuple[str, str | None]:
        glob = _arg(args, "glob", str)
        n, note = bounded("limit", _arg(args, "limit", int, DEFAULT_FILES_LIMIT), MAX_FILES_LIMIT)
        return baseline.list_files(self.root, glob, n), note

    def _read_file(self, args: dict) -> tuple[str, str | None]:
        path = _arg(args, "path", str, required=True)
        start = max(1, _arg(args, "start_line", int, 1))
        n, note = bounded(
            "max_lines", _arg(args, "max_lines", int, DEFAULT_READ_LINES), MAX_READ_LINES
        )
        return baseline.read_file(self.root, path, start, n), note

    def _grep(self, args: dict) -> tuple[str, str | None]:
        pattern = _arg(args, "pattern", str, required=True)
        path = _arg(args, "path", str)
        fixed = _arg(args, "fixed", bool, False)
        n, note = bounded("limit", _arg(args, "limit", int, DEFAULT_GREP_LIMIT), MAX_LIMIT)
        return baseline.grep(self.root, pattern, path, fixed, n), note

    def _git_diff(self, args: dict) -> tuple[str, str | None]:
        path = _arg(args, "path", str)
        context = _arg(args, "context", int, 3)
        clamped = max(0, min(context, MAX_DIFF_CONTEXT))
        note = None
        if clamped != context:
            note = f"[context={clip(str(context), 12)} clamped to {clamped}]"
        return baseline.git_diff(self.root, self.base, self.head, self.label, path, clamped), note

    def close(self) -> None:
        pass


class CompassTools:
    """The compass MCP server, in this process, through the MCP client protocol."""

    def __init__(self, root: Path, db_path: Path):
        from mcp import Client

        from compass.server import build_server

        self._stack = ExitStack()
        try:
            portal = self._stack.enter_context(start_blocking_portal())
            client = Client(build_server(root, db_path), mode="legacy")
            self._client = self._stack.enter_context(portal.wrap_async_context_manager(client))
            self._portal = portal
            listed = portal.call(self._client.list_tools).tools
        except BaseException:
            self._stack.close()
            raise
        self._definitions = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in listed
        ]
        self.names = {t.name for t in listed}

    def definitions(self) -> list[dict]:
        return self._definitions

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        if name not in self.names:
            return f"Unknown tool {clip(name, 60)}.", True
        try:
            result = self._portal.call(self._client.call_tool, name, arguments)
        except Exception as exc:  # protocol or validation errors from the MCP layer
            return clip(f"{name} failed: {exc}", 500), True
        text = "".join(c.text for c in result.content if c.type == "text")
        return text, bool(result.is_error)

    def close(self) -> None:
        self._stack.close()


class CombinedTools:
    """Several tool sets as one. Names must not overlap."""

    def __init__(self, *toolsets):
        self.toolsets = toolsets
        self._owner = {}
        for toolset in toolsets:
            for definition in toolset.definitions():
                if definition["name"] in self._owner:
                    raise ValueError(f"tool {definition['name']} defined twice")
                self._owner[definition["name"]] = toolset

    def definitions(self) -> list[dict]:
        return [d for t in self.toolsets for d in t.definitions()]

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        toolset = self._owner.get(name)
        if toolset is None:
            return f"Unknown tool {clip(name, 60)}.", True
        return toolset.call(name, arguments)

    def close(self) -> None:
        for toolset in self.toolsets:
            toolset.close()


class open_toolset(AbstractContextManager):
    """`with open_toolset(mode, ...) as tools:` builds the mode's tools and closes them."""

    def __init__(self, mode: str, root: Path, db_path: Path, base: str, head: str, label: str):
        if mode not in MODES:
            raise ValueError(f"unknown tools mode {mode!r}; choose from {', '.join(MODES)}")
        self.args = (mode, root, db_path, base, head, label)
        self.toolset = None

    def __enter__(self):
        mode, root, db_path, base, head, label = self.args
        if mode == "none":
            self.toolset = NoTools()
        elif mode == "baseline":
            self.toolset = BaselineTools(root, base, head, label)
        elif mode == "compass":
            self.toolset = CompassTools(root, db_path)
        else:
            self.toolset = CombinedTools(
                CompassTools(root, db_path), BaselineTools(root, base, head, label)
            )
        return self.toolset

    def __exit__(self, *exc):
        if self.toolset is not None:
            self.toolset.close()
        return False
