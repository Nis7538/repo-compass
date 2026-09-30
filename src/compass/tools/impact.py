"""diff_impact: what a change touches, and who outside it depends on what changed.

Steps:
1. Files: the merge base of base and head (like a pull request, `base...head`) is
   compared with head, a commit or the working tree, by blob id (gitrepo.changed_files).
2. Symbols: both versions of each changed Java/Python file are parsed and diffed
   (tools/changes.py). This part is exact.
3. Callers: each changed symbol still present is looked up in the index and its
   call sites found with the resolver (docs/adr/003), tier by tier, like
   find_references. A caller is inside the diff (its file changed too, so it was
   probably updated) or outside (untouched: the real risk). A removed symbol is put
   back into the resolver from its old version, so the call sites that would still
   bind to it are found: the uses it left dangling. Imports naming it are counted
   too. This part is approximate, the same way find_references is.
4. Rank, then cut (docs/tools.md): removed and signature changes with exact or
   likely callers outside the diff first, then body changes with such callers, then
   the rest, added symbols last. `possible` callers are counted and shown but never
   raise a symbol's rank: a method named `process` with 100 name-only matches must
   not top the list.

Limits: callers and importers come from the index, which follows the working tree,
so for a head commit other than HEAD they are the working tree's and the header says
so. No override or dispatch analysis: changing an interface method does not flag its
implementations. Field uses are not indexed. Java classes used from the same package
need no import, so importers undercount them.
"""

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from compass import gitrepo
from compass.gitrepo import FileChange, GitError
from compass.indexer.extract_python import module_name
from compass.indexer.walk import is_binary, language_of
from compass.store.db import get_meta
from compass.store.queries import SYMBOL_SELECT, SymbolRow, get_symbol, symbol_row
from compass.store.resolve import EXACT, LIKELY, POSSIBLE, RANK, Reference, Resolver
from compass.tools.caps import CAPS, MAX_LIMIT
from compass.tools.changes import (
    ADDED,
    BODY,
    REMOVED,
    SIGNATURE,
    Parsed,
    SymbolChange,
    Version,
    diff_symbols,
    parse,
)
from compass.tools.deps import import_target, type_modules
from compass.tools.rank import SIGNATURE_WIDTH, is_test_path
from compass.tools.references import rank_references
from compass.tools.render import (
    ELLIPSIS,
    Entry,
    clip,
    common_dir,
    fit_grouped,
    more_hint,
    truncated,
)

MAX_PARSED_FILES = 300
MAX_LOOKUPS = 200
EXAMPLE_CALLERS = 3
LIST_SHOWN = 5
LINE_WIDTH = 400
CHANGE_ORDER = (REMOVED, SIGNATURE, BODY, ADDED)
STRONG = (EXACT, LIKELY)
NOT_LOOKED_UP = "callers not looked up"


@dataclass
class Impact:
    change: SymbolChange
    note: str | None = None  # instead of callers: 'uses not indexed', 'not in the index'
    refs: list[Reference] = field(default_factory=list)  # ranked; for removed: dangling
    outside: list[Reference] = field(default_factory=list)  # refs from unchanged files
    imports: list[str] = field(default_factory=list)  # removed only: 'File.java:3'

    @property
    def risky(self) -> list[Reference]:
        """The references that count as evidence: exact or likely, outside the diff
        (for a removed symbol, anywhere: a dangling use is broken wherever it is)."""
        pool = self.refs if self.change.change == REMOVED else self.outside
        return [r for r in pool if r.confidence in STRONG]


def diff_impact(conn: sqlite3.Connection, base: str, head: str | None, limit: int) -> str:
    root = Path(get_meta(conn, "repo_root") or ".")
    head_label = head or "working tree"
    try:
        gitrepo.check_repo(root)
        base_sha = gitrepo.resolve_commit(root, base)
        head_sha = gitrepo.resolve_commit(root, head) if head else None
        current = gitrepo.head_commit(root)
        merge_base = gitrepo.merge_base(root, base_sha, head_sha or current or base_sha)
        if merge_base is None:
            return f"{base} and {head_label} share no history (no merge base)."
        files = gitrepo.changed_files(root, merge_base, head_sha)
    except GitError as exc:
        return clip(exc.message, 600)

    if not files:
        return f"No changes between {base} and {head_label}."
    code = [f for f in files if language_of(f.path)]
    other = len(files) - len(code)
    if not code:
        return f"No Java or Python files changed ({other} other file{_s(other)})."

    try:
        changes = _symbol_changes(root, merge_base, head_sha, code[:MAX_PARSED_FILES])
    except GitError as exc:
        return clip(exc.message, 600)
    changed_paths = {f.path for f in code}
    impacts = _impacts(conn, changes, changed_paths)
    ranked = sorted(impacts, key=_rank_key)

    tests = sum(1 for f in code if is_test_path(f.path))
    kinds = Counter(i.change.change for i in impacts)
    head_lines = [
        f"diff {base}...{head_label} (merge base {merge_base[:7]}): {len(code)} code"
        f" file{_s(len(code))} changed ({tests} test), {other} other file{_s(other)};"
        f" {len(impacts)} symbol{_s(len(impacts))} changed"
        + (f" ({_counts(kinds)})" if impacts else "")
    ]
    if head_sha is not None and head_sha != current:
        head_lines.append(
            f"note: callers and importers are from the working tree, not from {head}"
        )
    if len(code) > MAX_PARSED_FILES:
        head_lines.append(
            f"note: symbols compared in the first {MAX_PARSED_FILES} of {len(code)} code"
            " files (by path)"
        )
    skipped = sum(1 for i in impacts if i.note == NOT_LOOKED_UP)
    if skipped:
        head_lines.append(
            f"note: callers looked up for {MAX_LOOKUPS} changed symbols only, not for"
            f" {skipped} more (removed and signature changes go first)"
        )

    modules = sorted({c.module for c in changes if c.module} | _file_modules(conn, code))
    head_lines.append(_list_line(f"modules touched {len(modules)}", modules))
    if not impacts:
        head_lines.append(
            "no symbol changed: the edits are outside any class, method or function"
            " (imports, constants, module-level code)"
        )
        return "\n".join(head_lines)

    wanted = ranked[:limit]
    entries = [Entry(i.change.path, _entry_text(conn, i)) for i in wanted]
    prefix = common_dir([e.group for e in entries])
    tail = [_importers_line(conn, set(modules), changed_paths, prefix)]
    # fit_grouped places head lines before the entries; the importers line goes after
    # them, but its room must be reserved up front, so it is passed as a head line and
    # moved to the end.
    lines, shown = fit_grouped(head_lines + tail, entries, CAPS["diff_impact"])
    lines = head_lines + lines[len(head_lines) + len(tail) :] + tail
    if shown < len(ranked):
        cut = ranked[shown:]
        with_callers = sum(1 for i in cut if i.risky or i.imports)
        callers = (
            f"{with_callers} with callers outside the diff"
            if with_callers
            else "none has callers outside the diff"
        )
        detail = f"{_counts(Counter(i.change.change for i in cut))}; {callers}"
        hint = more_hint(len(ranked), shown, limit, MAX_LIMIT)
        lines.append(truncated(len(cut), detail, hint, "symbols"))
    return "\n".join(lines)


# --- 1-2: changed files and symbols -------------------------------------------------


def _symbol_changes(
    root: Path, base: str, head: str | None, files: list[FileChange]
) -> list[SymbolChange]:
    base_packages = _package_dirs(gitrepo.tree_files(root, base))
    head_listing = gitrepo.tree_files(root, head) if head else gitrepo.tracked_and_untracked(root)
    head_packages = _package_dirs(head_listing)

    base_blobs = gitrepo.read_blobs(root, [f.base_blob for f in files if f.base_blob])
    head_blobs = gitrepo.read_blobs(root, [f.head_blob for f in files if f.head_blob])
    before: list[Parsed] = []
    after: list[Parsed] = []
    for f in files:
        language = language_of(f.path)
        if f.base_blob is not None:
            data = base_blobs[f.base_blob]
            before.append(_parse(f.path, language, data, base_packages))
        if f.status != "deleted":
            data = head_blobs[f.head_blob] if f.head_blob else _read(root / f.path)
            after.append(_parse(f.path, language, data, head_packages))
    return diff_symbols(before, after)


def _package_dirs(paths) -> set[str]:
    return {str(PurePosixPath(p).parent) for p in paths if p.endswith("__init__.py")}


def _parse(path: str, language: str, data: bytes, packages: set[str]) -> Parsed:
    module = module_name(path, packages) if language == "python" else ""
    if is_binary(data):
        data = b""
    return parse(Version(path, language, module, data))


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def _file_modules(conn: sqlite3.Connection, files: list[FileChange]) -> set[str]:
    """Modules of changed files the parser gave no symbols (module-level edits only)."""
    paths = [f.path for f in files if f.status != "deleted"]
    found = set()
    for chunk in _chunks(paths):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(f"SELECT module FROM files WHERE path IN ({marks})", chunk)
        found.update(r["module"] for r in rows if r["module"])
    return found


# --- 3: callers ---------------------------------------------------------------------


def _impacts(
    conn: sqlite3.Connection, changes: list[SymbolChange], changed_paths: set[str]
) -> list[Impact]:
    resolver = Resolver(conn)
    rows = _index_rows(conn, {c.path for c in changes})
    virtual = _virtual_rows(conn, [c for c in changes if c.change in (REMOVED, SIGNATURE)])
    resolver.add_virtual(list(virtual.values()))

    impacts = [Impact(c) for c in changes]
    budget = MAX_LOOKUPS
    for impact in sorted(impacts, key=lambda i: CHANGE_ORDER.index(i.change.change)):
        c = impact.change
        if c.change == ADDED:
            continue
        if c.symbol.kind == "field":
            impact.note = "uses not indexed"
            continue
        if budget == 0:
            impact.note = NOT_LOOKED_UP
            continue
        budget -= 1
        if c.change == REMOVED:
            refs = resolver.find_references(virtual[id(c)])
            impact.imports = _imports_naming(conn, virtual[id(c)])
        else:
            target = _pick(rows.get((c.path, c.symbol.qualified_name, c.symbol.kind), []), c)
            if target is None:
                impact.note = "not in the index"
                continue
            refs = resolver.find_references(target)
            if c.change == SIGNATURE:
                # Calls written for the old declaration may no longer bind to the new one
                # (a Java call with the old argument count), and they are the ones most
                # likely broken; count callers of either version.
                refs = _union(refs, resolver.find_references(virtual[id(c)]))
        impact.refs = rank_references(refs)
        impact.outside = [r for r in impact.refs if r.ref.path not in changed_paths]
    return impacts


def _union(a: list[Reference], b: list[Reference]) -> list[Reference]:
    """Call sites in either list, each once, with its best tier."""
    best: dict[int, Reference] = {}
    for r in a + b:
        if r.ref.id not in best or RANK[r.confidence] > RANK[best[r.ref.id].confidence]:
            best[r.ref.id] = r
    return list(best.values())


def _index_rows(conn: sqlite3.Connection, paths: set[str]) -> dict[tuple, list[SymbolRow]]:
    """(path, qualified name, kind) -> index rows, for the symbols of the given files."""
    rows: dict[tuple, list[SymbolRow]] = {}
    for chunk in _chunks(sorted(paths)):
        marks = ",".join("?" * len(chunk))
        for r in conn.execute(SYMBOL_SELECT + f" WHERE f.path IN ({marks})", chunk):
            s = symbol_row(r)
            rows.setdefault((s.path, s.qualified_name, s.kind), []).append(s)
    return rows


def _pick(rows: list[SymbolRow], change: SymbolChange) -> SymbolRow | None:
    """The index row for a changed symbol: same signature if overloaded, else nearest."""
    if not rows:
        return None
    same = [s for s in rows if s.signature == change.symbol.signature]
    return min(same or rows, key=lambda s: abs(s.start_line - change.symbol.start_line))


def _virtual_rows(conn: sqlite3.Connection, changes: list[SymbolChange]) -> dict[int, SymbolRow]:
    """Old versions of removed and re-declared symbols as index rows, keyed by id(change).

    Ids are negative, so they never collide with real rows. A method's parent is its
    class's current row if the class still exists, or the class's own virtual row
    if the class was removed too.
    """
    paths = {c.path for c in changes}
    rows = conn.execute("SELECT id, path FROM files")
    file_ids = {r["path"]: r["id"] for r in rows if r["path"] in paths}
    removed_ids = {
        (c.symbol.qualified_name, c.symbol.kind): -(n + 1)
        for n, c in enumerate(changes)
        if c.change == REMOVED
    }
    virtual = {}
    for n, c in enumerate(changes):
        s = c.symbol if c.change == REMOVED else c.was
        parent_id = None
        if c.parent is not None:
            key = (c.parent.qualified_name, c.parent.kind)
            parent_id = removed_ids.get(key) or _current_id(conn, *key)
        virtual[id(c)] = SymbolRow(
            id=-(n + 1),
            file_id=file_ids.get(c.path, -1),
            path=c.path,
            language=language_of(c.path),
            module=c.module,
            parent_id=parent_id,
            kind=s.kind,
            name=s.name,
            qualified_name=s.qualified_name,
            signature=s.signature,
            param_count=s.param_count,
            is_varargs=s.is_varargs,
            decorators=tuple(s.decorators),
            bases=tuple(s.bases),
            doc=s.doc,
            start_line=s.start_line,
            end_line=s.end_line,
        )
    return virtual


def _current_id(conn: sqlite3.Connection, qualified_name: str, kind: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM symbols WHERE qualified_name = ? AND kind = ? ORDER BY id LIMIT 1",
        (qualified_name, kind),
    ).fetchone()
    return row["id"] if row else None


def _imports_naming(conn: sqlite3.Connection, target: SymbolRow) -> list[str]:
    """Import statements that name a removed symbol: 'File.java:3' for each."""
    qn = target.qualified_name
    rows = conn.execute(
        "SELECT f.path, i.line FROM imports i JOIN files f ON f.id = i.file_id"
        " WHERE f.language = ? AND (i.module || '.' || i.name = ?"
        " OR (i.is_static = 1 AND i.module = ?))"
        " ORDER BY f.path, i.line",
        (target.language, qn, qn),
    )
    return [f"{_file_name(r['path'])}:{r['line']}" for r in rows]


# --- 4: ranking and text --------------------------------------------------------------


def _rank_key(impact: Impact) -> tuple:
    c = impact.change
    strong = len(impact.risky) + len(impact.imports)
    if c.change == ADDED:
        group = 4
    elif c.change in (REMOVED, SIGNATURE):
        group = 0 if strong else 2
    else:
        group = 1 if strong else 3
    weak = len(impact.outside if c.change != REMOVED else impact.refs) - len(impact.risky)
    return (group, is_test_path(c.path), -strong, -weak, c.path, c.symbol.start_line)


def _entry_text(conn: sqlite3.Connection, impact: Impact) -> str:
    """One changed symbol: a first line, then indented detail lines."""
    c = impact.change
    s = c.symbol
    line = f"-{s.start_line}" if c.change == REMOVED else str(s.start_line)
    first = f"{line} {c.change} {s.kind} {_short(c)}"
    detail: list[str] = []
    if c.change == ADDED:
        first += f"  {clip(s.signature, SIGNATURE_WIDTH)}"
    if c.change == SIGNATURE:
        new, old = clip_pair(_declared(s, c.was), _declared(c.was, s), SIGNATURE_WIDTH)
        first += f"  {new}"
        detail.append(f"was: {old}")
    if c.change != ADDED:
        summary = impact.note or _callers_summary(impact)
        if c.change == SIGNATURE:
            detail.append(summary)
        else:
            first += f"  {summary}"
    examples = _examples(conn, impact)
    if examples:
        detail.append("<- " + ", ".join(examples))
    # Every part is already clipped (names come from source, and are short), so the
    # lines are joined as they are; clip() would also collapse the two-space gaps.
    return "\n".join([first] + ["    " + d for d in detail])


def _declared(symbol, other) -> str:
    """The signature, led by its decorators/annotations when only those changed."""
    if symbol.signature != other.signature or symbol.decorators == other.decorators:
        return symbol.signature
    return " ".join([*(f"@{d}" for d in symbol.decorators), symbol.signature])


def clip_pair(new: str, old: str, width: int) -> tuple[str, str]:
    """Clip two versions of a signature so the part that differs stays visible.

    Clipping both at the end would show two identical prefixes when a long signature
    changes near its end (a parameter added last). Instead both start a little
    before the first character that differs, with a leading '…'.
    """
    new, old = " ".join(new.split()), " ".join(old.split())
    if max(len(new), len(old)) <= width:
        return new, old
    first_diff = next(
        (i for i, (a, b) in enumerate(zip(new, old)) if a != b), min(len(new), len(old))
    )
    if first_diff < width - 20:
        return clip(new, width), clip(old, width)
    start = first_diff - 30
    return clip(ELLIPSIS + new[start:], width), clip(ELLIPSIS + old[start:], width)


def _callers_summary(impact: Impact) -> str:
    if impact.change.change == REMOVED:
        refs = impact.refs
        calls = f"{len(refs)} call{_s(len(refs))}" + (
            f" ({_tiers(refs)}{_name_only(refs)})" if refs else ""
        )
        imports = len(impact.imports)
        return f"dangling: {calls}, {imports} import{_s(imports)}"
    refs, outside = impact.refs, impact.outside
    text = f"callers {len(refs)}"
    if refs:
        text += f", outside the diff {len(outside)}"
    if outside:
        tests = sum(1 for r in outside if is_test_path(r.ref.path))
        tested = f"; {tests} in tests" if tests else ""
        text += f" ({_tiers(outside)}{_name_only(outside)}{tested})"
    return text


def _name_only(refs: list[Reference]) -> str:
    """', name matches only' when no reference is exact or likely."""
    return "" if any(r.confidence in STRONG for r in refs) else ", name matches only"


def _examples(conn: sqlite3.Connection, impact: Impact) -> list[str]:
    """Up to three calling symbols, best-ranked first, each once: 'Cart.add (Cart.java:44)'.

    Only exact and likely callers. A name-only match shown as an example reads as a real
    caller: in the M3 end-to-end run the model called Map.get sites the "blast radius"
    of a static JavaVersion.get.
    """
    shown: dict[str, str] = {}
    for r in impact.risky:
        where = f"{_file_name(r.ref.path)}:{r.ref.line}"
        if r.ref.enclosing_symbol_id is None:  # module-level code
            shown.setdefault(where, where)
        else:
            name = _caller_name(conn, r.ref.enclosing_symbol_id)
            shown.setdefault(name, f"{name} ({where})")
        if len(shown) == EXAMPLE_CALLERS:
            break
    examples = list(shown.values())
    if impact.change.change == REMOVED:
        room = EXAMPLE_CALLERS - len(examples)
        examples += [f"import at {where}" for where in impact.imports[:room]]
    return examples


def _caller_name(conn: sqlite3.Connection, symbol_id: int) -> str:
    row = get_symbol(conn, symbol_id)
    return _relative(row.qualified_name, row.module) if row else "?"


def _importers_line(
    conn: sqlite3.Connection, modules: set[str], changed_paths: set[str], prefix: str
) -> str:
    """Files outside the diff with an import of a touched module."""
    known = {r["module"] for r in conn.execute("SELECT DISTINCT module FROM files")} | modules
    type_module = type_modules(conn)
    importers = set()
    rows = conn.execute(
        "SELECT f.path, f.language, i.module, i.name FROM imports i"
        " JOIN files f ON f.id = i.file_id"
    )
    for r in rows:
        if r["path"] in changed_paths:
            continue
        target = import_target(r["language"], r["module"], r["name"], known, type_module)
        if target in modules:
            importers.add(r["path"])
    label = "importers of touched modules outside the diff"
    if not importers:
        return f"{label}: none"
    ordered = sorted(importers, key=lambda p: (is_test_path(p), p))
    tests = sum(1 for p in ordered if is_test_path(p))
    shown = [p.removeprefix(prefix) for p in ordered]
    count = f"{len(ordered)} file{_s(len(ordered))} ({tests} test{_s(tests)})"
    return _list_line(f"{label}: {count}", shown)


def _list_line(label: str, items: list[str]) -> str:
    if not items:
        return f"{label}: none"
    text = f"{label}: " + ", ".join(items[:LIST_SHOWN])
    if len(items) > LIST_SHOWN:
        text += f", +{len(items) - LIST_SHOWN}"
    return clip(text, LINE_WIDTH)


def _short(c: SymbolChange) -> str:
    return _relative(c.symbol.qualified_name, c.module)


def _relative(qualified_name: str, module: str) -> str:
    prefix = module + "." if module else ""
    return qualified_name.removeprefix(prefix) if prefix else qualified_name


def _tiers(refs: list[Reference]) -> str:
    counts = Counter(r.confidence for r in refs)
    return ", ".join(f"{t} {counts[t]}" for t in (EXACT, LIKELY, POSSIBLE) if counts[t])


def _counts(kinds: Counter) -> str:
    return ", ".join(f"{k} {kinds[k]}" for k in CHANGE_ORDER if kinds[k])


def _file_name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def _chunks(items: list[str], size: int = 500):
    for i in range(0, len(items), size):
        yield items[i : i + size]
