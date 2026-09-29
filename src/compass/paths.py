"""Where index databases live.

The index is never written inside the target repository (the tool is read-only
with respect to the repo). By default it goes into a per-user cache directory,
one file per repository, keyed by the repository's absolute path.
"""

import hashlib
import os
import sys
from pathlib import Path


def cache_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "repo-compass"


def default_db_path(repo_root: Path) -> Path:
    root = repo_root.resolve()
    # normcase so C:\Repo and c:\repo map to the same index on Windows.
    digest = hashlib.sha1(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:12]
    return cache_dir() / f"{root.name or 'root'}-{digest}.db"
