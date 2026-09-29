"""repo_summary: size, languages, biggest packages and index freshness in one screen.

Packages: a Java file's package, a Python file's package (the module itself for
__init__.py, otherwise the module minus its last part). Ranked by lines of code,
largest first.
"""

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from compass.store.db import get_meta
from compass.tools.caps import CAPS, MAX_LIMIT
from compass.tools.rank import is_test_path
from compass.tools.render import fit_lines, more_hint, truncated

MAX_ROOTS = 6


@dataclass
class _Package:
    files: int = 0
    lines: int = 0
    dirs: set[str] = field(default_factory=set)


def repo_summary(conn: sqlite3.Connection, freshness: str, limit: int) -> str:
    root = get_meta(conn, "repo_root") or "?"
    rows = conn.execute(
        "SELECT path, language, module, line_count, has_errors FROM files ORDER BY path"
    ).fetchall()
    symbols = conn.execute("SELECT count(*) FROM symbols").fetchone()[0]
    refs = conn.execute("SELECT count(*) FROM refs").fetchone()[0]

    languages: dict[str, int] = defaultdict(int)
    packages: dict[str, _Package] = defaultdict(_Package)
    total_lines = errors = tests = 0
    for r in rows:
        languages[r["language"]] += 1
        total_lines += r["line_count"]
        errors += r["has_errors"]
        tests += is_test_path(r["path"])
        package = _package(r["language"], r["path"], r["module"])
        pkg = packages[package]
        pkg.files += 1
        pkg.lines += r["line_count"]
        pkg.dirs.add(r["path"].rsplit("/", 1)[0] + "/" if "/" in r["path"] else "./")

    langs = ", ".join(f"{lang} {n:,}" for lang, n in sorted(languages.items()))
    head = [
        f"repo {root.replace(chr(92), '/')}  index: {freshness}",
        f"files {len(rows):,} ({langs or 'none'}; tests {tests:,})  lines {total_lines:,}"
        f"  symbols {symbols:,}  call sites {refs:,}  parse errors {errors}",
    ]
    if not rows:
        return "\n".join(head + ["No .java or .py files found."])

    ranked = sorted(packages.items(), key=lambda kv: (-kv[1].lines, kv[0]))
    # A package whose directory is <source root>/<package as path> needs no directory of
    # its own: the source roots are printed once and the rest follows from the name.
    roots: set[str] = set()
    entries = []
    for name, p in ranked[:limit]:
        derived = {d: _source_root(d, name) for d in p.dirs}
        roots.update(r for r in derived.values() if r is not None)
        odd = sorted(d for d, r in derived.items() if r is None)
        where = "  " + ", ".join(odd[:2]) + (f" +{len(odd) - 2} dirs" if len(odd) > 2 else "")
        files = f"{p.files} file{'s' if p.files != 1 else ''}"
        entries.append(
            f"  {name or '(no package)'}{where if odd else ''}  {files} {p.lines:,} lines"
        )
    head.append(f"packages by lines ({min(limit, len(ranked))} of {len(ranked)}):")
    if roots:
        shown_roots = sorted(roots)[:MAX_ROOTS]
        extra = f" +{len(roots) - MAX_ROOTS} more" if len(roots) > MAX_ROOTS else ""
        head.append(
            "source roots (package dir = root + package path): " + ", ".join(shown_roots) + extra
        )
    lines, shown = fit_lines(head, entries, CAPS["repo_summary"])
    if shown < len(ranked):
        lines.append(
            truncated(len(ranked) - shown, None, more_hint(len(ranked), shown, limit, MAX_LIMIT))
        )
    return "\n".join(lines)


def _source_root(directory: str, package: str) -> str | None:
    """'java/src/main/java/' for directory 'java/src/main/java/com/x/' and package 'com.x'."""
    tail = package.replace(".", "/") + "/"
    if not package or not directory.endswith(tail):
        return None
    if len(directory) > len(tail) and directory[-len(tail) - 1] != "/":
        return None  # 'xcom/x/' is not under a root that holds 'com/x/'
    return directory[: -len(tail)] or "./"


def _package(language: str, path: str, module: str) -> str:
    if language == "python" and not path.endswith("__init__.py"):
        return module.rpartition(".")[0]
    return module
