"""module_dependencies: the import graph between modules, and its cycles.

Nodes are modules as the index records them: a Java package, a Python dotted
module (one per file; a package is its __init__.py). Edges come from import
statements, weighted by how many import statements make them:

- Java `import a.b.C` / `import a.b.*` -> package a.b; a static import
  `import static a.b.C.m` -> the package of type a.b.C; an import of a nested type
  maps to the package of its outer type.
- Python `from p import sub` -> p.sub when that is a module, else p;
  `import p.q` -> p.q.
- Anything not in the index is external, collapsed to its first two segments for
  Java (java.util) and its first segment for Python (os, requests).

Test files (tools/rank.is_test_path) are left out of the graph. In Java they
usually share packages with the code they test, and their imports of fixtures
and test-only packages would otherwise show up as production dependencies and
cycles (on apache/commons-lang they joined 16 packages into one cycle).

Cycles are strongly connected components with more than one module (Tarjan),
largest first, each shown as one concrete loop through the component.

Limits: Java classes used from the same package, or written fully qualified
without an import, create no edge. Python imports inside functions count the
same as top-level ones. Dynamic imports (importlib, reflection) are invisible.
"""

import sqlite3
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field

from compass.indexer.models import TYPE_KINDS
from compass.tools.caps import CAPS, MAX_LIMIT
from compass.tools.rank import is_test_path
from compass.tools.render import clip, fit_lines, more_hint, truncated

MAX_CYCLES = 3
CYCLE_WIDTH = 300
TOP_IMPORTED = 5


@dataclass
class Graph:
    modules: dict[str, int]  # module -> file count
    edges: Counter = field(default_factory=Counter)  # (src, dst) -> import statements
    external: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    test_files: int = 0  # left out of the graph


def build_graph(conn: sqlite3.Connection) -> Graph:
    files = conn.execute("SELECT id, path, module FROM files WHERE module != ''").fetchall()
    kept = {f["id"] for f in files if not is_test_path(f["path"])}
    modules: Counter = Counter(f["module"] for f in files if f["id"] in kept)
    kinds = ",".join("?" * len(TYPE_KINDS))
    type_module = {
        r["qualified_name"]: r["module"]
        for r in conn.execute(
            "SELECT s.qualified_name, s.file_id, f.module FROM symbols s"
            f" JOIN files f ON f.id = s.file_id WHERE s.kind IN ({kinds})",
            sorted(TYPE_KINDS),
        )
        if r["file_id"] in kept
    }
    graph = Graph(dict(modules), test_files=len(files) - len(kept))
    rows = conn.execute(
        "SELECT i.file_id, f.module AS src, f.language, i.module, i.name, i.is_static"
        " FROM imports i JOIN files f ON f.id = i.file_id WHERE f.module != ''"
    )
    for r in rows:
        if r["file_id"] not in kept:
            continue
        dst = _target(r["language"], r["module"], r["name"], modules, type_module)
        if dst is None:
            graph.external[r["src"]][_external_name(r["language"], r["module"])] += 1
        elif dst != r["src"]:
            graph.edges[(r["src"], dst)] += 1
    return graph


def _target(language, module, name, modules, type_module) -> str | None:
    if language == "python":
        if name and name != "*" and f"{module}.{name}" in modules:
            return f"{module}.{name}"
        return module if module in modules else None
    if module in modules:
        return module
    return type_module.get(module)  # static import, or import of a nested type


def _external_name(language: str, module: str) -> str:
    parts = module.split(".")
    return ".".join(parts[:1] if language == "python" else parts[:2])


def module_dependencies(conn: sqlite3.Connection, module: str | None, limit: int) -> str:
    graph = build_graph(conn)
    if not graph.modules:
        return "No modules in the index."
    if module is None or not module.strip():
        return _overview(graph, limit)
    return _one_module(graph, module.strip(), limit)


def _overview(graph: Graph, limit: int) -> str:
    cycles = find_cycles(graph)
    head = [
        f"{len(graph.modules)} modules, {len(graph.edges)} internal import edges,"
        f" {len(cycles)} cycle{'s' if len(cycles) != 1 else ''} (edge weight = import"
        f" statements; {graph.test_files} test files left out)"
    ]
    importers: dict[str, set[str]] = defaultdict(set)
    for src, dst in graph.edges:
        importers[dst].add(src)
    if importers:
        top = sorted(importers.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:TOP_IMPORTED]
        head.append("most imported: " + ", ".join(f"{m} (by {len(s)})" for m, s in top))
    for cycle in cycles[:MAX_CYCLES]:
        head.append(_cycle_line(graph, cycle))
    if len(cycles) > MAX_CYCLES:
        head.append(truncated(len(cycles) - MAX_CYCLES, None, None, "cycles"))
    if not graph.edges:
        return "\n".join(head + ["no internal import edges"])

    ranked = sorted(graph.edges.items(), key=lambda kv: (-kv[1], kv[0]))
    head.append("top edges:")
    entries = [f"  {src} -> {dst} {n}" for (src, dst), n in ranked[:limit]]
    lines, shown = fit_lines(head, entries, CAPS["module_dependencies"])
    if shown < len(ranked):
        hint = more_hint(len(ranked), shown, limit, MAX_LIMIT) + "; or pass module="
        lines.append(truncated(len(ranked) - shown, None, hint, "edges"))
    return "\n".join(lines)


def _one_module(graph: Graph, spec: str, limit: int) -> str:
    members = _members(graph, spec)
    if not members:
        needle = spec.rstrip(".*").lower()
        similar = sorted(m for m in graph.modules if needle in m.lower())[:5]
        hint = f" Similar: {', '.join(similar)}." if similar else ""
        return (
            f'No module "{spec}" in the import graph (test files are left out).{hint}'
            " Call with no module for an overview."
        )

    group = len(members) > 1
    files = sum(graph.modules[m] for m in members)
    file_count = f"{files} file{'s' if files != 1 else ''}"
    if group:
        base = spec[:-2] if spec.endswith(".*") else spec
        head = [f"module {base}.* ({len(members)} modules, {file_count})"]
    else:
        head = [f"module {spec} ({file_count})"]

    out: Counter = Counter()
    into: Counter = Counter()
    for (src, dst), n in graph.edges.items():
        if src in members and dst not in members:
            out[dst] += n
        elif dst in members and src not in members:
            into[src] += n
    external: Counter = Counter()
    for m in members:
        external.update(graph.external.get(m, {}))

    head.append(_ranked_list(f"imports {len(out)} internal", out, limit))
    head.append(_ranked_list(f"imported by {len(into)}", into, limit))
    head.append(_ranked_list(f"external {len(external)}", external, limit))

    cycles = [c for c in find_cycles(graph) if c & members]
    if not cycles:
        head.append("cycles: none")
    for cycle in cycles[:MAX_CYCLES]:
        head.append(_cycle_line(graph, cycle, min(cycle & members)))
    if len(cycles) > MAX_CYCLES:
        head.append(truncated(len(cycles) - MAX_CYCLES, None, None, "cycles"))
    lines, _ = fit_lines(head, [], CAPS["module_dependencies"])
    return "\n".join(lines)


def _members(graph: Graph, spec: str) -> set[str]:
    """'a.b' -> {'a.b'} if it is a module; 'a.b.*' or a non-module prefix -> its submodules."""
    base = spec[:-2] if spec.endswith(".*") else spec
    if not spec.endswith(".*") and base in graph.modules:
        return {base}
    return {m for m in graph.modules if m == base or m.startswith(base + ".")}


def _ranked_list(label: str, counts: Counter, limit: int) -> str:
    if not counts:
        return f"{label}"
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    text = f"{label}: " + ", ".join(f"{m} {n}" for m, n in ranked[:limit])
    if len(ranked) > limit:
        text += " " + truncated(len(ranked) - limit, None, f"limit={min(len(ranked), MAX_LIMIT)}")
    return text


def find_cycles(graph: Graph) -> list[set[str]]:
    """Strongly connected components with 2+ modules, largest first (iterative Tarjan)."""
    succ = _successors(graph)
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[set[str]] = []
    counter = 0

    for start in sorted(graph.modules):
        if start in index:
            continue
        work = [(start, 0)]
        while work:
            node, i = work.pop()
            if i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            children = succ.get(node, [])
            if i < len(children):
                work.append((node, i + 1))
                child = children[i]
                if child not in index:
                    work.append((child, 0))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            if low[node] == index[node]:
                component = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(component)
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return sorted(components, key=lambda c: (-len(c), min(c)))


def _cycle_line(graph: Graph, cycle: set[str], start: str | None = None) -> str:
    """'cycle: a -> b -> a', or 'cycle among 16 modules, e.g. a -> b -> a' when the loop
    shown does not pass through every module of the component."""
    loop = _loop_text(graph, cycle, start)
    if loop.count(" -> ") == len(cycle):
        return f"cycle: {loop}"
    return f"cycle among {len(cycle)} modules, e.g. {loop}"


def _successors(graph: Graph) -> dict[str, list[str]]:
    succ: dict[str, list[str]] = defaultdict(list)
    for src, dst in sorted(graph.edges):
        succ[src].append(dst)
    return succ


def _loop_text(graph: Graph, cycle: set[str], start: str | None = None) -> str:
    """One concrete loop through start inside the component: 'a -> b -> a'."""
    start = start or min(cycle)
    succ = _successors(graph)
    # Breadth-first search back to start, staying inside the component.
    parents: dict[str, str] = {}
    queue = deque([start])
    found = None
    while queue and found is None:
        node = queue.popleft()
        for dst in succ.get(node, []):
            if dst not in cycle:
                continue
            if dst == start:
                found = node
                break
            if dst not in parents:
                parents[dst] = node
                queue.append(dst)
    path = [start]
    node = found
    while node is not None and node != start:
        path.append(node)
        node = parents.get(node)
    loop = [start] + path[:0:-1] + [start]
    return clip(" -> ".join(loop), CYCLE_WIDTH)
