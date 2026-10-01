"""Plain file and text tools: the review agent's baseline condition.

They give an agent roughly what a coding agent has without compass: list files, read a
file, grep, and the change's diff. M5 compares an agent using these with one using the
compass tools, so they follow the same output rules (ADR-005): plain text, a hard cap per
response (BASELINE_CAPS), and an explicit `[truncated: N more ...]` line when something
is cut. They know nothing about symbols, call sites or imports.

All four read the working tree, the same view the compass index has, except git_diff,
which compares two commits. Everything is read-only:

- Files are only those git would list (tracked, plus untracked and not ignored), so
  nothing under .git/ and nothing ignored, such as a .env file, can be read.
- read_file refuses absolute paths, '..', symlinks and binary files, and checks that
  the resolved path is still inside the root.
- grep and git_diff go through gitrepo.run (no shell, no config-driven programs,
  a timeout).

Problems the agent can fix (a wrong path, a bad regex) come back as a short message,
not an exception, the same as the compass tools.
"""

import difflib
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from compass import gitrepo
from compass.gitrepo import GitError
from compass.indexer.walk import is_binary
from compass.tools.caps import BASELINE_CAPS, MAX_FILES_LIMIT, MAX_LIMIT
from compass.tools.render import (
    CHARS_PER_TOKEN,
    ELLIPSIS,
    HEADROOM,
    Entry,
    clip,
    fit_grouped,
    fit_lines,
    more_hint,
    truncated,
)

# Source lines are cut, not collapsed: indentation matters when reading code.
READ_LINE_WIDTH = 300
GREP_LINE_WIDTH = 200
DIFF_LINE_WIDTH = 300
MAX_FILE_BYTES = 2_000_000
MAX_SUGGESTIONS = 5


def list_files(root: Path, glob: str | None, limit: int) -> str:
    """Files in the repository, grouped by directory, directories in path order.

    glob is matched against the whole path with fnmatch, where '*' also crosses '/':
    '*.py' is every Python file, 'src/*' everything under src/. A glob ending in '/'
    is a directory.
    """
    try:
        paths = gitrepo.tracked_and_untracked(root)
    except GitError as exc:
        return exc.message
    if glob:
        pattern = glob + "*" if glob.endswith("/") else glob
        paths = [p for p in paths if fnmatchcase(p, pattern)]
    paths.sort(key=lambda p: (p.rpartition("/")[0], p))
    matched = f' matching "{clip(glob, 80)}"' if glob else ""
    if not paths:
        return f"No files{matched}."
    head = [f"{len(paths)} files{matched}"]
    lines, shown = _fit_paths(head, paths[:limit], BASELINE_CAPS["list_files"])
    if shown < len(paths):
        hint = more_hint(len(paths), shown, limit, MAX_FILES_LIMIT)
        lines.append(truncated(len(paths) - shown, unit="files", hint=hint))
    return "\n".join(lines)


def _fit_paths(head: list[str], paths: list[str], cap_tokens: int) -> tuple[list[str], int]:
    """Each directory once ('src/compass/'), its files indented under it."""
    budget = cap_tokens * CHARS_PER_TOKEN - HEADROOM - sum(len(line) + 1 for line in head)
    lines = list(head)
    current = None
    shown = 0
    for path in paths:
        directory, _, name = path.rpartition("/")
        directory = directory + "/" if directory else "./"
        cost = len(name) + 3 + (len(directory) + 1 if directory != current else 0)
        budget -= cost
        if budget < 0:
            break
        if directory != current:
            lines.append(directory)
            current = directory
        lines.append("  " + name)
        shown += 1
    return lines, shown


def read_file(root: Path, path: str, start_line: int, max_lines: int) -> str:
    """Numbered lines start_line.. of one file, at most max_lines of them."""
    rel = _clean(path)
    if rel is None:
        return f'Refusing path "{clip(path, 100)}": use a path relative to the repository.'
    try:
        listed = gitrepo.tracked_and_untracked(root)
    except GitError as exc:
        return exc.message
    if rel not in set(listed):
        return _not_found(rel, listed)
    full = root / rel
    try:
        resolved = full.resolve()
        if full.is_symlink() or not resolved.is_relative_to(root.resolve()):
            return f"Refusing {rel}: it is a symlink or leads outside the repository."
        if not full.is_file():
            return f"Not a file: {rel}."
        size = full.stat().st_size
        if size > MAX_FILE_BYTES:
            return f"Refusing {rel}: {size} bytes is too large to read."
        data = full.read_bytes()
    except OSError as exc:
        return f"Cannot read {rel}: {exc.strerror or exc}."
    if is_binary(data):
        return f"{rel} is a binary file ({len(data)} bytes)."

    text = data.decode("utf-8", errors="replace").splitlines()
    total = len(text)
    if total == 0:
        return f"{rel} is empty."
    if start_line > total:
        return f"{rel} has {total} lines; start_line={start_line} is past the end."
    window = text[start_line - 1 : start_line - 1 + max_lines]
    width = len(str(start_line - 1 + len(window)))
    numbered = [
        f"{start_line + i:>{width}}  {_cut(line, READ_LINE_WIDTH)}"
        for i, line in enumerate(window)
    ]
    last = start_line - 1 + len(window)
    head = [f"{rel}  lines {start_line}-{last} of {total}"]
    lines, shown = fit_lines(head, numbered, BASELINE_CAPS["read_file"])
    if shown < len(window):
        lines[0] = f"{rel}  lines {start_line}-{start_line - 1 + shown} of {total}"
    end = start_line - 1 + shown
    if end < total:
        hint = f"start_line={end + 1} continues"
        if shown < len(window):
            hint = "output cap reached; " + hint
        lines.append(truncated(total - end, unit="lines", hint=hint))
    return "\n".join(lines)


def _clean(path: str) -> str | None:
    """A repo-relative POSIX path, or None if it is absolute or climbs out with '..'."""
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ":" in pure.parts[0] or ".." in pure.parts:
        return None
    return str(pure)


def _not_found(rel: str, listed: list[str]) -> str:
    name = rel.rsplit("/", 1)[-1]
    similar = [p for p in listed if p.endswith("/" + rel) or p == rel]
    if not similar:
        similar = [p for p in listed if p.rsplit("/", 1)[-1] == name]
    if not similar:
        names = {p.rsplit("/", 1)[-1]: p for p in listed}
        close = difflib.get_close_matches(name, list(names), n=MAX_SUGGESTIONS, cutoff=0.75)
        similar = [names[n] for n in close]
    message = f"Not a file in this repository: {clip(rel, 100)}."
    if similar:
        shown = ", ".join(similar[:MAX_SUGGESTIONS])
        extra = len(similar) - MAX_SUGGESTIONS
        message += f" Did you mean: {shown}" + (f", +{extra} more" if extra > 0 else "") + "?"
    return clip(message, 600)


def _cut(line: str, width: int) -> str:
    line = line.rstrip()
    return line if len(line) <= width else line[: width - 1] + ELLIPSIS


def grep(root: Path, pattern: str, path: str | None, fixed: bool, limit: int) -> str:
    """Matching lines, grouped by file, in path order. pattern is an extended regex
    unless fixed."""
    if not pattern:
        return "Empty pattern: pass the text or regular expression to search for."
    where = None
    if path:
        where = _clean(path)
        if where is None:
            return f'Refusing path "{clip(path, 100)}": use a path relative to the repository.'
    try:
        hits = gitrepo.grep(root, pattern, where, fixed)
    except GitError as exc:
        return clip(exc.message, 600)
    shown_pattern = clip(pattern, 80)
    scope = f" in {clip(where, 80)}" if where else ""
    if not hits:
        return f'No matches for "{shown_pattern}"{scope}.'
    files = len({h.path for h in hits})
    head = [f'{len(hits)} matches in {files} files for "{shown_pattern}"{scope}']
    entries = [Entry(h.path, f"{h.line}: {clip(h.text, GREP_LINE_WIDTH)}") for h in hits]
    lines, shown = fit_grouped(head, entries[:limit], BASELINE_CAPS["grep"])
    if shown < len(hits):
        hint = more_hint(len(hits), shown, limit, MAX_LIMIT)
        lines.append(truncated(len(hits) - shown, unit="matches", hint=hint))
    return "\n".join(lines)


def git_diff(root: Path, base: str, head: str, label: str, path: str | None, context: int) -> str:
    """The unified diff base..head, of every file or of one path.

    base and head are commits (the reviewer passes the merge base and the head);
    label is how the header names the comparison.
    """
    where = None
    if path:
        where = _clean(path)
        if where is None:
            return f'Refusing path "{clip(path, 100)}": use a path relative to the repository.'
    try:
        text = gitrepo.patch(root, base, head, where, context)
    except GitError as exc:
        return clip(exc.message, 600)
    scope = f" {clip(where, 100)}" if where else ""
    if not text.strip():
        return f"No changes{scope} in {label}."
    body = [_cut(line, DIFF_LINE_WIDTH) for line in text.splitlines()]
    lines, shown = fit_lines([f"diff {label}{scope}"], body, BASELINE_CAPS["git_diff"])
    if shown < len(body):
        cut = body[shown:]
        unseen = [line.split(" b/", 1)[-1] for line in cut if line.startswith("diff --git ")]
        detail = None
        if unseen:
            more = len(unseen) - MAX_SUGGESTIONS
            named = ", ".join(unseen[:MAX_SUGGESTIONS]) + (f", +{more} more" if more > 0 else "")
            detail = f"{len(unseen)} files not shown: {named}"
        hint = "output cap reached; " + (
            "pass path= for one file" if not where else "read_file shows the new version"
        )
        lines.append(truncated(len(cut), detail, hint, unit="lines"))
    return "\n".join(lines)
