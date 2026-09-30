"""Read-only git access: refs, listings, working-tree hashing, history."""

import subprocess

import pytest

from compass import gitrepo
from compass.gitrepo import FileChange, GitError, Touch
from tests.gitrepo import ScriptedRepo

LF = b"class A:\n    pass\n"
LF_NO_EOL = b"class A:\n    pass"
CRLF = b"class A:\r\n    pass\r\n"
CRLF_NO_EOL = b"class A:\r\n    pass"


@pytest.fixture
def repo(tmp_path):
    return ScriptedRepo(tmp_path / "repo")


def _hash_object(repo: ScriptedRepo, path: str, *config: str) -> str:
    return subprocess.run(
        ["git", *config, "hash-object", *(["--no-filters"] if not config else []), path],
        cwd=repo.root,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize("data", [LF, LF_NO_EOL, CRLF, CRLF_NO_EOL, b""])
def test_blob_id_matches_git_hash_object(repo, data):
    (repo.root / "f.py").write_bytes(data)
    assert gitrepo.blob_id(data) == _hash_object(repo, "f.py")


@pytest.mark.parametrize("data", [CRLF, CRLF_NO_EOL])
def test_crlf_file_also_matches_its_autocrlf_blob(repo, data):
    # With core.autocrlf=true git commits CRLF files with LF. blob_ids offers both ids.
    (repo.root / "f.py").write_bytes(data)
    committed = _hash_object(repo, "f.py", "-c", "core.autocrlf=true")
    assert committed == gitrepo.blob_id(data.replace(b"\r\n", b"\n"))
    assert gitrepo.blob_ids(data) == (_hash_object(repo, "f.py"), committed)


@pytest.mark.parametrize("data", [LF, LF_NO_EOL])
def test_lf_file_has_one_blob_id(data):
    assert gitrepo.blob_ids(data) == (gitrepo.blob_id(data),)


def test_worktree_checked_out_with_crlf_is_unchanged(repo):
    repo.write("a.py", "x = 1\ny = 2\n")
    base = repo.commit("a")
    (repo.root / "a.py").write_bytes(b"x = 1\r\ny = 2\r\n")  # what autocrlf=true checks out
    assert gitrepo.changed_files(repo.root, base, None) == []


def test_changed_files_between_commits(repo):
    repo.write("keep.py", "k = 1\n")
    repo.write("edit.py", "e = 1\n")
    repo.write("gone.py", "g = 1\n")
    repo.write("old/name.py", "n = 1\n")
    base = repo.commit("one")
    repo.write("edit.py", "e = 2\n")
    repo.remove("gone.py")
    repo.write("new.py", "n = 2\n")
    repo.rename("old/name.py", "moved/name.py")
    head = repo.commit("two")

    changes = gitrepo.changed_files(repo.root, base, head)
    assert [(c.path, c.status) for c in changes] == [
        ("edit.py", "modified"),
        ("gone.py", "deleted"),
        ("moved/name.py", "added"),  # no rename detection: a move is delete + add
        ("new.py", "added"),
        ("old/name.py", "deleted"),
    ]
    edit = changes[0]
    blobs = gitrepo.read_blobs(repo.root, [edit.base_blob, edit.head_blob])
    assert blobs == {edit.base_blob: b"e = 1\n", edit.head_blob: b"e = 2\n"}


def test_changed_files_against_the_working_tree(repo):
    repo.write("edit.py", "e = 1\n")
    repo.write("gone.py", "g = 1\n")
    repo.write("same.py", "s = 1\n")
    repo.write(".gitignore", "ignored.py\n")
    base = repo.commit("one")
    repo.write("edit.py", "e = 2\n")  # not staged: the working tree is what counts
    repo.remove("gone.py")
    repo.write("untracked.py", "u = 1\n")
    repo.write("ignored.py", "i = 1\n")

    assert gitrepo.changed_files(repo.root, base, None) == [
        FileChange("edit.py", "modified", gitrepo.blob_id(b"e = 1\n"), None),
        FileChange("gone.py", "deleted", gitrepo.blob_id(b"g = 1\n"), None),
        FileChange("untracked.py", "added", None, None),
    ]


def test_paths_are_relative_to_a_subdirectory_root(repo):
    repo.write("svc/src/a.py", "a = 1\n")
    repo.write("other/b.py", "b = 1\n")
    base = repo.commit("one")
    repo.write("svc/src/a.py", "a = 2\n")
    repo.write("other/b.py", "b = 2\n")
    head = repo.commit("two")
    svc = repo.root / "svc"
    assert list(gitrepo.tree_files(svc, head)) == ["src/a.py"]
    assert [c.path for c in gitrepo.changed_files(svc, base, head)] == ["src/a.py"]
    assert [c.path for c in gitrepo.changed_files(svc, base, None)] == ["src/a.py"]
    commits = gitrepo.log_touches(svc, ["HEAD"], 100)
    assert [c.touches for c in commits] == [(Touch("M", "src/a.py"),), (Touch("A", "src/a.py"),)]


def test_reading_the_working_tree_leaves_git_index_untouched(repo):
    repo.write("a.py", "a = 1\n")
    base = repo.commit("one")
    repo.write("a.py", "a = 2\n")
    index = repo.root / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    gitrepo.changed_files(repo.root, base, None)
    gitrepo.log_touches(repo.root, ["HEAD"], 10)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_resolve_commit_and_unknown_refs(repo):
    repo.write("a.py", "a = 1\n")
    sha = repo.commit("one")
    for name in ["m2-server", "feature/x"]:
        repo.branch(name)
    assert gitrepo.resolve_commit(repo.root, "main") == sha
    assert gitrepo.resolve_commit(repo.root, sha[:7]) == sha
    with pytest.raises(GitError) as err:
        gitrepo.resolve_commit(repo.root, "mian")
    assert err.value.message == (
        'Unknown ref "mian". Branches: main, feature/x, m2-server.'
        " Also works: a tag, a commit, HEAD~3."
    )


def test_refs_starting_with_a_dash_are_refused(repo):
    repo.write("a.py", "a = 1\n")
    repo.commit("one")
    with pytest.raises(GitError, match='leading "-"'):
        gitrepo.resolve_commit(repo.root, "--output=x")
    assert gitrepo.try_commit(repo.root, "--all") is None


def test_not_a_repository(tmp_path):
    with pytest.raises(GitError) as err:
        gitrepo.check_repo(tmp_path)
    assert err.value.message.startswith(f"Not a git repository: {tmp_path.as_posix()}.")


def test_merge_base_of_unrelated_histories_is_none(repo):
    repo.write("a.py", "a = 1\n")
    main = repo.commit("one")
    repo.git("checkout", "-q", "--orphan", "other")
    repo.write("b.py", "b = 1\n")
    other = repo.commit("unrelated")
    assert gitrepo.merge_base(repo.root, main, other) is None


def test_log_follows_renames_and_skips_merges(repo):
    repo.write("a.py", "a = 1\n")
    repo.commit("one", "2026-01-01T00:00:00+00:00")
    repo.branch("side")
    repo.rename("a.py", "b.py")
    repo.commit("rename", "2026-01-02T00:00:00+00:00")
    repo.checkout("side")
    repo.write("c.py", "c = 1\n")
    repo.commit("side", "2026-01-03T00:00:00+00:00")
    repo.checkout("main")
    repo.merge("side", "merge", "2026-01-04T00:00:00+00:00")

    commits = gitrepo.log_touches(repo.root, ["HEAD"], 100)
    assert [c.touches for c in commits] == [
        (Touch("A", "c.py"),),
        (Touch("R", "b.py", "a.py"),),
        (Touch("A", "a.py"),),
    ]
    assert commits[0].time == 1767398400  # 2026-01-03T00:00:00Z
    assert len(gitrepo.log_touches(repo.root, ["HEAD"], 2)) == 2


def test_approxidate(repo):
    repo.write("a.py", "a = 1\n")
    repo.commit("one")
    assert gitrepo.approxidate(repo.root, "2026-01-01 00:00:00 +0000") == 1767225600
