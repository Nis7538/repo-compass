"""End-to-end indexing and incremental reindex behaviour."""

import os
from pathlib import Path

import pytest

from compass.indexer.pipeline import index_repo
from compass.store.db import open_index
from tests.helpers import init_git_repo

MODELS = "src/inventory/models.py"
SERVICES = "src/inventory/services.py"
ALL_PY = [
    "scripts/seed.py",
    "src/inventory/__init__.py",
    "src/inventory/helpers.py",
    MODELS,
    SERVICES,
    "src/inventory/utils.py",
]


@pytest.fixture
def repo(copy_fixture) -> Path:
    return copy_fixture("python")


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "index.db"


def _rows(db: Path, sql: str) -> list[tuple]:
    conn = open_index(db)
    try:
        return [tuple(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def _rows_not_in(db: Path, table: str, path: str) -> list[tuple]:
    return _rows(
        db,
        f"SELECT t.* FROM {table} t JOIN files f ON f.id = t.file_id "
        f"WHERE f.path != '{path}' ORDER BY t.id",
    )


def test_cold_index(repo, db):
    result = index_repo(repo, db)
    assert result.discovery == "walk"
    assert sorted(result.parsed) == ALL_PY
    assert result.files == len(ALL_PY)
    assert result.lines > 100
    modules = dict(_rows(db, "SELECT path, module FROM files"))
    assert modules[MODELS] == "inventory.models"
    assert modules["src/inventory/__init__.py"] == "inventory"
    assert modules["scripts/seed.py"] == "seed"


def test_reindex_without_changes_parses_nothing(repo, db):
    index_repo(repo, db)
    result = index_repo(repo, db)
    assert result.parsed == [] and result.removed == []
    assert result.unchanged == len(ALL_PY)


def test_modifying_one_file_reparses_only_that_file(repo, db):
    index_repo(repo, db)
    before = {t: _rows_not_in(db, t, SERVICES) for t in ("symbols", "imports", "refs")}

    with open(repo / SERVICES, "a", encoding="utf-8") as f:
        f.write("\n\ndef added_later():\n    return build()\n")
    result = index_repo(repo, db)

    assert result.parsed == [SERVICES]
    assert result.unchanged == len(ALL_PY) - 1
    # Rows of every other file are untouched: same ids, same content.
    for table, rows in before.items():
        assert _rows_not_in(db, table, SERVICES) == rows, table
    names = _rows(db, "SELECT name FROM symbols WHERE name = 'added_later'")
    assert names == [("added_later",)]


def test_same_size_same_mtime_edit_is_still_detected(repo, db):
    """A rewrite in the same mtime tick as the last run is caught by the racy check."""
    path = repo / "src/inventory/utils.py"
    os.utime(path)  # written "just now", right before the index run
    st = path.stat()
    index_repo(repo, db)
    path.write_bytes(path.read_bytes().replace(b"def save(obj)", b"def keep(obj)"))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert path.stat().st_size == st.st_size

    assert index_repo(repo, db).parsed == ["src/inventory/utils.py"]


def test_touch_without_content_change_parses_nothing(repo, db):
    index_repo(repo, db)
    path = repo / MODELS
    new_mtime = path.stat().st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(new_mtime, new_mtime))

    result = index_repo(repo, db)
    assert result.parsed == []
    assert _rows(db, f"SELECT mtime_ns FROM files WHERE path = '{MODELS}'") == [(new_mtime,)]


def test_deleted_file_is_removed_with_its_rows(repo, db):
    index_repo(repo, db)
    (repo / "src/inventory/utils.py").unlink()
    result = index_repo(repo, db)
    assert result.removed == ["src/inventory/utils.py"]
    assert _rows(db, "SELECT count(*) FROM symbols WHERE qualified_name LIKE 'inventory.utils%'")[
        0
    ] == (0,)


def test_rename_is_remove_plus_add(repo, db):
    index_repo(repo, db)
    (repo / "src/inventory/utils.py").rename(repo / "src/inventory/tools.py")
    result = index_repo(repo, db)
    assert result.removed == ["src/inventory/utils.py"]
    assert result.parsed == ["src/inventory/tools.py"]
    assert _rows(
        db, "SELECT qualified_name FROM symbols WHERE name = 'save' AND parent_id IS NULL"
    )[0] == ("inventory.tools.save",)


def test_new_package_init_reparses_files_whose_module_changed(repo, db):
    index_repo(repo, db)
    (repo / "scripts/__init__.py").write_text("")
    result = index_repo(repo, db)
    assert sorted(result.parsed) == ["scripts/__init__.py", "scripts/seed.py"]
    assert _rows(db, "SELECT module FROM files WHERE path = 'scripts/seed.py'") == [
        ("scripts.seed",)
    ]


def test_binary_content_is_skipped(repo, db):
    (repo / "src/inventory/blob.py").write_bytes(b"\x00\x01\x02binary")
    result = index_repo(repo, db)
    assert result.skipped == ["src/inventory/blob.py"]
    assert _rows(db, "SELECT count(*) FROM files WHERE path LIKE '%blob.py'") == [(0,)]


def test_java_and_python_together_in_git_repo(copy_fixture, db):
    repo = copy_fixture("")  # whole fixtures dir: java + python
    init_git_repo(repo)
    result = index_repo(repo, db)
    assert result.discovery == "git"
    languages = dict(_rows(db, "SELECT language, count(*) FROM files GROUP BY language"))
    assert languages == {"java": 10, "python": len(ALL_PY)}
    broken = _rows(db, "SELECT has_errors FROM files WHERE path LIKE '%Broken.java'")
    assert broken == [(1,)]


def test_indexing_never_writes_into_the_repo(repo, db):
    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            str(p.relative_to(repo)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in repo.rglob("*")
        }

    before = snapshot()
    index_repo(repo, db)
    (repo / MODELS).write_text((repo / MODELS).read_text() + "\n")
    before[MODELS.replace("/", os.sep)] = snapshot()[MODELS.replace("/", os.sep)]
    index_repo(repo, db)
    assert snapshot() == before


def test_index_is_one_transaction(repo, db, monkeypatch):
    """A crash mid-run leaves the previous index intact."""
    index_repo(repo, db)
    (repo / MODELS).write_text("def replaced():\n    pass\n")

    def boom(*args, **kwargs):
        raise RuntimeError("crash while extracting")

    monkeypatch.setattr("compass.indexer.pipeline.extract_python", boom)
    with pytest.raises(RuntimeError):
        index_repo(repo, db)
    assert _rows(db, "SELECT count(*) FROM symbols WHERE name = 'Item'") == [(1,)]
    assert _rows(db, "SELECT count(*) FROM symbols WHERE name = 'replaced'") == [(0,)]
