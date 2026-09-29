"""Find the source files to index.

Inside a git work tree we ask git for the file list (`git ls-files`), so every
ignore rule git knows about (nested .gitignore, .git/info/exclude, global
excludes) is applied exactly as git applies it. Outside git we fall back to a
plain directory walk, which does NOT read .gitignore files.

In both cases vendored/build directories, symlinks, oversized files and
non-Java/Python files are skipped. Binary content is detected later, when the
file is read (see is_binary).
"""

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

LANGUAGES = {".java": "java", ".py": "python"}

SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        ".eggs",
        ".mypy_cache",
        ".pytest_cache",
        ".gradle",
        "__pycache__",
        "node_modules",
        "site-packages",
        "third_party",
        "vendor",
        "build",
        "dist",
        "target",
    }
)

# Anything bigger is almost certainly generated code, not something to navigate.
MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class SourceFile:
    path: str  # repo-relative, forward slashes
    abs_path: Path
    language: str
    size: int
    mtime_ns: int


def discover(root: Path) -> tuple[list[SourceFile], str]:
    """Return the indexable files under root, sorted by path, and the method used."""
    rel_paths = _git_files(root)
    method = "git"
    if rel_paths is None:
        rel_paths = _walk_files(root)
        method = "walk"

    files = []
    for rel in sorted(set(rel_paths)):
        pure = PurePosixPath(rel)
        language = LANGUAGES.get(pure.suffix)
        if language is None or any(part in SKIP_DIRS for part in pure.parts[:-1]):
            continue
        abs_path = root / rel
        try:
            st = abs_path.lstat()
        except FileNotFoundError:  # tracked by git but deleted in the work tree
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            continue  # symlinks, submodule dirs, huge files
        files.append(SourceFile(rel, abs_path, language, st.st_size, st.st_mtime_ns))
    return files, method


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def _git_files(root: Path) -> list[str] | None:
    """Tracked plus untracked-but-not-ignored files, relative to root. None if not git."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, NotADirectoryError):  # git not installed / bad root
        return None
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _walk_files(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel_dir = Path(dirpath).relative_to(root)
        for name in filenames:
            out.append((rel_dir / name).as_posix())
    return out
