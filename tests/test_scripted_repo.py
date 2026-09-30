"""The scripted git repo helper gives the same SHAs on every machine."""

from tests.gitrepo import ScriptedRepo


def _history(repo: ScriptedRepo) -> list[str]:
    repo.write("a.py", "def f():\n    return 1\n")
    first = repo.commit("add a", "2026-01-05T09:00:00+00:00")
    repo.branch("feature")
    repo.checkout("feature")
    repo.write("b.py", "import a\n")
    second = repo.commit("add b", "2026-01-06T09:00:00+00:00")
    repo.checkout("main")
    repo.write("a.py", "def f():\n    return 2\n")
    third = repo.commit("change a", "2026-01-07T09:00:00+00:00")
    merge = repo.merge("feature", "merge feature", "2026-01-08T09:00:00+00:00")
    return [first, second, third, merge]


def test_same_script_gives_the_same_shas_in_two_places(tmp_path):
    one = _history(ScriptedRepo(tmp_path / "one"))
    two = _history(ScriptedRepo(tmp_path / "two"))
    assert one == two
    assert len(set(one)) == 4


def test_shas_are_literal_constants(tmp_path):
    # If this fails on some machine, a setting leaked into the repo (config, line endings);
    # every test that asserts a short SHA would fail there too.
    shas = _history(ScriptedRepo(tmp_path / "repo"))
    assert [s[:7] for s in shas] == ["e526509", "6b1f493", "8358de7", "9b5923f"]


def test_merge_makes_a_merge_commit(tmp_path):
    repo = ScriptedRepo(tmp_path / "repo")
    _history(repo)
    parents = repo.git("log", "-1", "--format=%P").split()
    assert len(parents) == 2
