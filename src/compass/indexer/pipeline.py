"""Index a repository: discover -> detect changes -> parse changed files -> store.

Change detection per file, cheapest check first:
1. size and mtime equal to what the index recorded -> unchanged, file not even read;
2. otherwise read and hash; same sha256 -> unchanged (only the stored mtime is updated);
3. otherwise re-extract and replace that file's rows.
Files in the index that were not discovered this run are deleted (cascading to their rows).

"Racy" files, modified within the same few seconds as the previous run, always get
hashed: a write landing in the same mtime tick as the last index would otherwise be
missed (git handles its index the same way).
"""

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from compass.indexer.extract_java import extract_java
from compass.indexer.extract_python import extract_python, module_name
from compass.indexer.models import FileExtract
from compass.indexer.walk import SourceFile, discover, is_binary
from compass.paths import default_db_path
from compass.store.db import (
    FileRecord,
    connect,
    delete_file,
    get_meta,
    load_file_states,
    set_meta,
    update_file_stat,
    write_file,
)

RACY_WINDOW_NS = 2_000_000_000


@dataclass
class IndexResult:
    root: Path
    db_path: Path
    discovery: str  # 'git' or 'walk'
    files: int = 0  # indexable files found this run
    parsed: list[str] = field(default_factory=list)  # (re)extracted this run
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # binary content
    unchanged: int = 0
    lines: int = 0  # total lines of the files parsed this run
    elapsed_s: float = 0.0


def index_repo(root: Path, db_path: Path | None = None) -> IndexResult:
    started = time.perf_counter()
    started_ns = time.time_ns()
    root = root.resolve()
    db_path = db_path or default_db_path(root)
    files, method = discover(root)
    result = IndexResult(root, db_path, method, files=len(files))

    package_dirs = {
        str(PurePosixPath(f.path).parent) for f in files if f.path.endswith("__init__.py")
    }

    conn = connect(db_path)
    try:
        with conn:  # one transaction for the whole run
            last_run_ns = int(get_meta(conn, "last_indexed_ns") or 0)
            known = load_file_states(conn)
            for source in files:
                state = known.pop(source.path, None)
                module = (
                    module_name(source.path, package_dirs) if source.language == "python" else None
                )
                # A Python file whose module name changed (an __init__.py appeared or
                # vanished) must be reparsed even if its bytes are the same.
                module_ok = state is not None and (module is None or state.module == module)
                racy = source.mtime_ns >= last_run_ns - RACY_WINDOW_NS
                if (
                    module_ok
                    and not racy
                    and state.size == source.size
                    and state.mtime_ns == source.mtime_ns
                ):
                    result.unchanged += 1
                    continue

                data = source.abs_path.read_bytes()
                if is_binary(data):
                    result.skipped.append(source.path)
                    if state is not None:
                        delete_file(conn, state.id)
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if module_ok and state.hash == digest:
                    update_file_stat(conn, state.id, source.size, source.mtime_ns)
                    result.unchanged += 1
                    continue

                extract = _extract(source, data, module)
                line_count = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
                record = FileRecord(
                    source.path, source.language, digest, source.size, source.mtime_ns, line_count
                )
                write_file(conn, record, extract)
                result.parsed.append(source.path)
                result.lines += line_count

            for path, state in known.items():
                delete_file(conn, state.id)
                result.removed.append(path)

            set_meta(conn, "repo_root", str(root))
            set_meta(conn, "last_indexed_ns", str(started_ns))
    finally:
        conn.close()

    result.elapsed_s = time.perf_counter() - started
    return result


def _extract(source: SourceFile, data: bytes, module: str | None) -> FileExtract:
    if source.language == "java":
        return extract_java(data)
    return extract_python(data, module or "", source.path.endswith("__init__.py"))
