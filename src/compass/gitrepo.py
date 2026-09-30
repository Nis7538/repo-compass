"""Read-only access to git, for the tools that need history (diff_impact, hotspots).

Every git process starts in run(), which makes these guarantees:

- No shell, arguments as a list. A ref from the agent that starts with '-' is
  refused before it reaches git, where it would be read as an option.
- Nothing from the repository's config gets to run a program. A repo's .git/config
  can name commands that git runs on its own: an fsmonitor hook, an external diff,
  textconv and clean filters (named in .gitattributes), a pager, gpg for signature
  checks. run() turns off fsmonitor and signature display with `-c`, and passes
  --no-pager. No call here asks git for a patch, so external diffs and textconv
  never run; `git log` passes --no-ext-diff --no-textconv anyway. Clean filters run
  whenever git reads a working-tree file, and no flag turns them off, so git is
  never asked to read one: working-tree files are hashed here, in Python
  (blob_id), and compared with the committed blob ids.
- Nothing is written. GIT_OPTIONAL_LOCKS=0 stops commands that would otherwise
  refresh .git/index as a side effect, and no command used here writes anything else.
- Bounded time: each git call has a timeout.

Paths are relative to the directory git runs in (the index root), which may be a
subdirectory of the repository, the same as the indexer's `git ls-files`.

Failures raise GitError, whose message is written for the agent.
"""

import difflib
import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_S = 30
SAFE_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.quotepath=off",
    "-c",
    "color.ui=false",
    "-c",
    "log.showSignature=false",
)
MAX_BRANCHES_SHOWN = 5
REGULAR_FILE_MODES = frozenset({"100644", "100755"})


class GitError(Exception):
    """A git problem, with a message that tells the agent what to do about it."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class GitCommandFailed(GitError):
    def __init__(self, args: tuple[str, ...], returncode: int, stderr: bytes):
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        first = detail[0] if detail else f"exit code {returncode}"
        super().__init__(f"git {args[0]} failed: {first}")
        self.returncode = returncode


def run(root: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Run one read-only git command in root and return its stdout."""
    cmd = ["git", "--no-pager", *SAFE_CONFIG, *args]
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            cmd, cwd=root, input=stdin, env=env, capture_output=True, timeout=TIMEOUT_S
        )
    except FileNotFoundError as exc:  # no git, or root does not exist
        if not root.is_dir():
            raise GitError(f"Directory not found: {root}.") from exc
        raise GitError("git is not installed or not on PATH.") from exc
    except NotADirectoryError as exc:
        raise GitError(f"Not a directory: {root}.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} took longer than {TIMEOUT_S}s.") from exc
    if proc.returncode != 0:
        raise GitCommandFailed(args, proc.returncode, proc.stderr)
    return proc.stdout


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").strip()


# --- repository and refs -------------------------------------------------------------


def check_repo(root: Path) -> None:
    """Raise GitError unless root is inside a git work tree."""
    try:
        run(root, "rev-parse", "--is-inside-work-tree")
    except GitCommandFailed:
        raise GitError(
            f"Not a git repository: {root.as_posix()}. diff_impact and hotspots need git;"
            " the other tools work without it."
        ) from None


def try_commit(root: Path, ref: str) -> str | None:
    """Full SHA of the commit ref names, or None if it names none."""
    if not ref or ref.startswith("-"):
        return None
    try:
        return _text(run(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"))
    except GitCommandFailed:
        return None


def resolve_commit(root: Path, ref: str) -> str:
    """Full SHA of the commit ref names. Unknown refs raise GitError listing branches."""
    if ref.startswith("-"):
        raise GitError(f'Refusing ref "{ref}": git would read a leading "-" as an option.')
    sha = try_commit(root, ref)
    if sha is None:
        raise GitError(unknown_ref_message(root, ref))
    return sha


def unknown_ref_message(root: Path, ref: str) -> str:
    names = branches(root)
    close = difflib.get_close_matches(ref, names, n=MAX_BRANCHES_SHOWN, cutoff=0.5)
    ordered = close + [b for b in names if b not in close]
    shown = ", ".join(ordered[:MAX_BRANCHES_SHOWN])
    if len(ordered) > MAX_BRANCHES_SHOWN:
        shown += f", +{len(ordered) - MAX_BRANCHES_SHOWN} more"
    listed = f" Branches: {shown}." if names else ""
    return f'Unknown ref "{ref}".{listed} Also works: a tag, a commit, HEAD~3.'


def branches(root: Path) -> list[str]:
    out = run(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return sorted(_text(out).splitlines())


def head_commit(root: Path) -> str | None:
    """SHA of HEAD, or None in a repository with no commits yet."""
    return try_commit(root, "HEAD")


def merge_base(root: Path, a: str, b: str) -> str | None:
    """The best common ancestor of two commits, or None if they share no history."""
    try:
        return _text(run(root, "merge-base", a, b))
    except GitCommandFailed as exc:
        if exc.returncode == 1:
            return None
        raise


def is_shallow(root: Path) -> bool:
    return _text(run(root, "rev-parse", "--is-shallow-repository")) == "true"


def commit_time(root: Path, sha: str) -> int:
    """Committer time of a commit, as a Unix timestamp."""
    return int(_text(run(root, "log", "-1", "--no-show-signature", "--format=%ct", sha)))


def approxidate(root: Path, text: str) -> int:
    """Unix timestamp git reads text as ('1 year ago', '2026-01-01').

    Git's date parser never fails: text it cannot read at all comes back as now.
    """
    out = _text(run(root, "rev-parse", f"--since={text}"))
    return int(out.removeprefix("--max-age="))


# --- file listings -------------------------------------------------------------------


def tracked_and_untracked(root: Path) -> list[str]:
    """Files in the work tree that git would list: tracked, plus untracked and not ignored.

    Names only; git reads no file content for this.
    """
    out = run(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({p for p in out.decode("utf-8", errors="replace").split("\0") if p})


def tree_files(root: Path, commit: str) -> dict[str, str]:
    """path -> blob id of every regular file in commit, under root.

    Symlinks and submodules are left out, the same as the indexer leaves them out.
    """
    out = run(root, "ls-tree", "-r", "-z", commit)
    files = {}
    for entry in out.decode("utf-8", errors="replace").split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        mode, kind, blob = meta.split(" ")
        if kind == "blob" and mode in REGULAR_FILE_MODES:
            files[path] = blob
    return files


def read_blobs(root: Path, blobs: list[str]) -> dict[str, bytes]:
    """Content of each blob id, read with one `git cat-file --batch` process."""
    wanted = list(dict.fromkeys(blobs))
    if not wanted:
        return {}
    out = run(root, "cat-file", "--batch", stdin="".join(b + "\n" for b in wanted).encode())
    contents: dict[str, bytes] = {}
    pos = 0
    for blob in wanted:
        header_end = out.index(b"\n", pos)
        header = out[pos:header_end].decode().split(" ")
        pos = header_end + 1
        if len(header) != 3:  # '<id> missing'
            raise GitError(f"git object {blob} is missing from the repository.")
        size = int(header[2])
        contents[blob] = out[pos : pos + size]
        pos += size + 1  # content is followed by a newline
    return contents


# --- comparing a commit with the working tree ----------------------------------------


def blob_id(data: bytes) -> str:
    """The id git gives these bytes as a blob: sha1 of 'blob <size>\\0' + data."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def blob_ids(data: bytes) -> tuple[str, ...]:
    """Blob ids that working-tree bytes may have been committed as.

    The raw bytes, and if they contain CRLF, the bytes with CRLF turned into LF: a
    checkout with core.autocrlf=true (common on Windows) writes CRLF files whose
    committed blobs have LF. Other checkout conversions (an `ident` or
    `working-tree-encoding` attribute, smudge filters) are not undone, so such files
    look changed.
    """
    raw = blob_id(data)
    if b"\r\n" not in data:
        return (raw,)
    return (raw, blob_id(data.replace(b"\r\n", b"\n")))


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str  # 'added' | 'deleted' | 'modified'
    base_blob: str | None  # None when added
    head_blob: str | None  # None when deleted, or when head is the working tree


def changed_files(root: Path, base: str, head: str | None) -> list[FileChange]:
    """Files that differ between commit base and commit head (or the working tree).

    Compares blob ids path by path; no rename detection, so a moved file is one
    deletion and one addition. With head=None, working-tree files are hashed here
    instead of by git (see the module docstring); untracked files that are not
    ignored count as added.
    """
    before = tree_files(root, base)
    changes = []
    if head is not None:
        after = tree_files(root, head)
        for path in sorted(before.keys() | after.keys()):
            old, new = before.get(path), after.get(path)
            if old != new:
                changes.append(FileChange(path, _status(old, new), old, new))
        return changes

    present = set()
    for path in tracked_and_untracked(root):
        data = _read_regular(root / path)
        if data is None:
            continue  # deleted from the work tree, or not a regular file
        present.add(path)
        old = before.get(path)
        if old is None:
            changes.append(FileChange(path, "added", None, None))
        elif old not in blob_ids(data):
            changes.append(FileChange(path, "modified", old, None))
    for path in before.keys() - present:
        changes.append(FileChange(path, "deleted", before[path], None))
    return sorted(changes, key=lambda c: c.path)


def _status(old: str | None, new: str | None) -> str:
    if old is None:
        return "added"
    if new is None:
        return "deleted"
    return "modified"


def _read_regular(path: Path) -> bytes | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        return path.read_bytes()
    except OSError:
        return None


# --- history -------------------------------------------------------------------------


@dataclass(frozen=True)
class Touch:
    status: str  # git's letter: A, M, D, R, T, ...
    path: str  # the path after the commit
    old_path: str | None = None  # for renames: the path before


@dataclass(frozen=True)
class Commit:
    sha: str
    time: int  # committer time, Unix
    touches: tuple[Touch, ...]


def log_touches(root: Path, revisions: list[str], max_commits: int) -> list[Commit]:
    """Non-merge commits that touched files under root, newest first, with the files.

    Renames are detected (-M), so a rename shows as one R touch with the old path.
    """
    out = run(
        root,
        "log",
        "--no-merges",
        "--no-show-signature",
        "--no-ext-diff",
        "--no-textconv",
        "--relative",
        "--name-status",
        "-M",
        "-z",
        f"--max-count={max_commits}",
        "--format=%x01%H %ct",
        *revisions,
        "--",
        ".",
    )
    commits = []
    for chunk in out.decode("utf-8", errors="replace").split("\x01")[1:]:
        header, _, rest = chunk.partition("\0")
        sha, ct = header.split(" ")
        fields = rest.lstrip("\n").split("\0")
        touches = []
        i = 0
        while i < len(fields) and fields[i]:
            status = fields[i]
            if status[0] in "RC":
                touches.append(Touch(status[0], fields[i + 2], fields[i + 1]))
                i += 3
            else:
                touches.append(Touch(status[0], fields[i + 1]))
                i += 2
        commits.append(Commit(sha, int(ct), tuple(touches)))
    return commits
