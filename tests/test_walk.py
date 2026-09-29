"""File discovery: git-aware listing, fallback walk and skip rules."""

from pathlib import Path

from compass.indexer.walk import MAX_FILE_BYTES, discover, is_binary
from tests.helpers import git, init_git_repo


def _write(root: Path, rel: str, text: str = "x = 1\n") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _paths(root: Path) -> list[str]:
    files, _ = discover(root)
    return [f.path for f in files]


def _make_tree(root: Path) -> None:
    _write(root, "app/main.py")
    _write(root, "app/Service.java", "class Service {}\n")
    _write(root, "app/notes.txt")
    _write(root, "vendor/lib/dep.py")
    _write(root, "node_modules/pkg/x.py")
    _write(root, "app/__pycache__/main.py")


def test_walk_fallback_outside_git(tmp_path):
    _make_tree(tmp_path)
    files, method = discover(tmp_path)
    assert method == "walk"
    assert [f.path for f in files] == ["app/Service.java", "app/main.py"]
    assert {f.language for f in files} == {"java", "python"}


def test_git_respects_gitignore_including_nested(tmp_path):
    _make_tree(tmp_path)
    _write(tmp_path, ".gitignore", "generated/\n")
    _write(tmp_path, "generated/gen.py")
    _write(tmp_path, "app/.gitignore", "local_*.py\n")
    _write(tmp_path, "app/local_settings.py")
    init_git_repo(tmp_path)
    _write(tmp_path, "app/untracked.py")  # untracked but not ignored: included

    files, method = discover(tmp_path)
    assert method == "git"
    assert [f.path for f in files] == ["app/Service.java", "app/main.py", "app/untracked.py"]


def test_git_skips_tracked_file_deleted_from_worktree(tmp_path):
    _write(tmp_path, "a.py")
    _write(tmp_path, "b.py")
    init_git_repo(tmp_path)
    (tmp_path / "b.py").unlink()
    assert _paths(tmp_path) == ["a.py"]


def test_git_listing_from_subdirectory_is_relative_to_it(tmp_path):
    _write(tmp_path, "svc/src/a.py")
    _write(tmp_path, "other/b.py")
    init_git_repo(tmp_path)
    assert _paths(tmp_path / "svc") == ["src/a.py"]


def test_skips_oversized_files(tmp_path):
    _write(tmp_path, "small.py")
    _write(tmp_path, "huge.py", "#" * (MAX_FILE_BYTES + 1))
    assert _paths(tmp_path) == ["small.py"]


def test_vendored_dirs_skipped_even_when_tracked(tmp_path):
    _write(tmp_path, "src/a.py")
    _write(tmp_path, "third_party/b.py")
    init_git_repo(tmp_path)
    git(tmp_path, "status")  # sanity: still a valid repo
    assert _paths(tmp_path) == ["src/a.py"]


def test_is_binary():
    assert is_binary(b"abc\0def")
    assert not is_binary("print('héllo')\n".encode())
