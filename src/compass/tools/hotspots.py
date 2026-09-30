"""hotspots: files that change often and hold a lot of code, where bugs tend to collect.

score = churn x size, per file:

- churn: non-merge commits in the window that touched the file (`git log --no-merges
  --name-status -M`). Renames are followed forward, so commits made under an old
  name count for the file's current path. Only files in the index count; deleted
  files drop out.
- size: lines inside outermost methods, functions and constructors, from the
  index (a method nested in another is not counted twice). Imports, constants and
  data declarations are left out, so a 900-line constants file does not outrank
  real logic.

Ties go to the file with more commits, then by path. Each line also names the
file's longest method (where to point get_symbol next) and the date of its latest
commit in the window.

`since` is a ref ('v1.2', 'main~50': the commits since..HEAD) if it names a
commit, otherwise a date git understands ('6 months ago', '2026-01-01'; a bare
date means midnight UTC). Default: one year back.

Limits: per file, not per method; commits are counted, not lines changed, so a
mass reformat counts like any other commit; uncommitted edits are not churn; sizes
come from the working tree; a shallow clone undercounts (the header says so); at
most MAX_COMMITS commits are read (the header says when that bound is hit).
"""

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from compass import gitrepo
from compass.gitrepo import Commit, GitError
from compass.store.db import get_meta
from compass.tools.caps import CAPS, MAX_LIMIT
from compass.tools.rank import is_test_path
from compass.tools.render import clip, common_dir, fit_lines, more_hint, truncated

DEFAULT_SINCE = "1 year ago"
MAX_COMMITS = 5000
CALLABLE_KINDS = frozenset({"method", "function", "constructor"})
_BARE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class FileSize:
    lines: int = 0  # inside outermost callables
    largest: str | None = None  # 'OrderService.checkout'
    largest_lines: int = 0


def hotspots(
    conn: sqlite3.Connection, since: str | None, limit: int, include_tests: bool = False
) -> str:
    root = Path(get_meta(conn, "repo_root") or ".")
    since = (since or "").strip() or DEFAULT_SINCE
    try:
        gitrepo.check_repo(root)
        if gitrepo.head_commit(root) is None:
            return "No commits yet."
        ref = gitrepo.try_commit(root, since)
        if ref is not None:
            start = gitrepo.commit_time(root, ref)
            window = f"{since} ({_date(start)})"
            revisions = [f"{ref}..HEAD"]
        else:
            spec = since + " 00:00:00 +0000" if _BARE_DATE.fullmatch(since) else since
            start = gitrepo.approxidate(root, spec)
            window = _date(start) if _date(start) == since else f'{_date(start)} ("{since}")'
            revisions = [f"--max-age={start}", "HEAD"]
        commits = gitrepo.log_touches(root, revisions, MAX_COMMITS)
        shallow = gitrepo.is_shallow(root)
    except GitError as exc:
        return clip(exc.message, 600)

    if not commits:
        return (
            f'No commits since "{since}" (read as {_date(start)}). since takes a date git'
            ' understands ("6 months ago", "2026-01-01") or a ref ("v1.2").'
        )

    churn, last = count_churn(commits)
    sizes, indexed = _sizes(conn)
    changed = [p for p in churn if p in indexed]
    tests = [p for p in changed if is_test_path(p)]
    kept = changed if include_tests else [p for p in changed if not is_test_path(p)]

    count = f"{len(commits)} commit{_s(len(commits))}"
    if len(commits) >= MAX_COMMITS:
        count += " (the most read; older ones not counted)"
    head = [
        f"hotspots since {window}: {count} (merges skipped),"
        f" {len(changed)} of {len(indexed)} indexed files changed; "
        + (f"tests included ({_files(len(tests))})" if include_tests else _left_out(len(tests)))
    ]
    if shallow:
        head.append("note: shallow clone, commits before its cut are missing")
    head.append("score = commits x lines inside methods/functions, per file. Renames followed.")

    def score(path: str) -> int:
        return churn[path] * sizes.get(path, FileSize()).lines

    ranked = sorted(kept, key=lambda p: (-score(p), -churn[p], p))
    wanted = ranked[:limit]
    prefix = common_dir(wanted)
    if prefix:
        head.append(f"paths under {prefix}")
    entries = [_line(p, prefix, churn[p], sizes.get(p, FileSize()), last[p]) for p in wanted]
    lines, shown = fit_lines(head, entries, CAPS["hotspots"])
    if shown < len(ranked):
        hint = more_hint(len(ranked), shown, limit, MAX_LIMIT)
        cut = len(ranked) - shown
        lines.append(truncated(cut, None, hint, "file" + _s(cut)))
    return "\n".join(lines)


def count_churn(commits: list[Commit]) -> tuple[Counter, dict[str, int]]:
    """Commits per current path, and the time of the latest one. commits: newest first.

    Walking back in time, a rename old -> new means older commits that touched `old`
    count for whatever `new` is called today. Before a path was added (or renamed
    into), commits touching it were about some other, earlier file, so they count for
    nothing.
    """
    now_called: dict[str, str | None] = {}  # path at some time -> path today, or None
    churn: Counter = Counter()
    last: dict[str, int] = {}
    for commit in commits:
        for path in {_current(t.path, now_called) for t in commit.touches} - {None}:
            churn[path] += 1
            last.setdefault(path, commit.time)
        for t in commit.touches:
            if t.status == "R":
                now_called[t.old_path] = _current(t.path, now_called)
            if t.status in ("A", "R"):
                now_called[t.path] = None
    return churn, last


def _current(path: str, now_called: dict[str, str | None]) -> str | None:
    return now_called.get(path, path)


def _sizes(conn: sqlite3.Connection) -> tuple[dict[str, FileSize], set[str]]:
    """Per indexed file: lines inside outermost callables, and its longest one."""
    rows = conn.execute(
        "SELECT s.id, s.parent_id, s.kind, s.qualified_name, s.start_line, s.end_line,"
        " f.path, f.module FROM symbols s JOIN files f ON f.id = s.file_id"
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    sizes: dict[str, FileSize] = {}
    for r in rows:
        if r["kind"] not in CALLABLE_KINDS or _inside_callable(r, by_id):
            continue
        n = r["end_line"] - r["start_line"] + 1
        size = sizes.setdefault(r["path"], FileSize())
        size.lines += n
        if n > size.largest_lines:
            prefix = r["module"] + "." if r["module"] else ""
            size.largest = r["qualified_name"].removeprefix(prefix)
            size.largest_lines = n
    indexed = {r["path"] for r in conn.execute("SELECT path FROM files")}
    return sizes, indexed


def _inside_callable(row: sqlite3.Row, by_id: dict[int, sqlite3.Row]) -> bool:
    parent = by_id.get(row["parent_id"])
    while parent is not None:
        if parent["kind"] in CALLABLE_KINDS:
            return True
        parent = by_id.get(parent["parent_id"])
    return False


def _line(path: str, prefix: str, commits: int, size: FileSize, last: int) -> str:
    text = f"  {commits * size.lines} = {commits} x {size.lines}  {path.removeprefix(prefix)}"
    if size.largest:
        text += f"  largest {clip(size.largest, 80)} {size.largest_lines} lines,"
    else:
        text += " "
    return text + f" last {_date(last)}"


def _left_out(tests: int) -> str:
    return f"left out: {tests} test file{_s(tests)}"


def _files(n: int) -> str:
    return f"{n} file{_s(n)}"


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def _date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).strftime("%Y-%m-%d")
