"""The baseline tools: plain list, read, grep and diff, capped like the compass tools."""

import os

import pytest

from compass.tools.baseline import git_diff, grep, list_files, read_file
from compass.tools.caps import BASELINE_CAPS
from compass.tools.render import estimate_tokens
from tests.gitrepo import ScriptedRepo


@pytest.fixture
def repo(tmp_path):
    repo = ScriptedRepo(tmp_path / "repo")
    repo.write(".gitignore", ".env\n")
    repo.write("app/orders.py", "def total(items):\n    return sum(items)\n")
    repo.write("app/util/money.py", "RATE = 2\n\n\ndef convert(x):\n    return x * RATE\n")
    repo.write("README.md", "# demo\n")
    repo.write("logo.png", "\0PNG")
    repo.write(".env", "SECRET=hunter2\n")
    repo.base = repo.commit("one")
    repo.write("app/orders.py", "def total(items, tax=0):\n    return sum(items) + tax\n")
    repo.write("app/new.py", "x = 1\n")
    repo.head_sha = repo.commit("two")
    return repo


def test_list_files_groups_by_directory(repo):
    assert list_files(repo.root, None, 100) == "\n".join(
        [
            "6 files",
            "./",
            "  .gitignore",
            "  README.md",
            "  logo.png",
            "app/",
            "  new.py",
            "  orders.py",
            "app/util/",
            "  money.py",
        ]
    )


def test_list_files_glob_and_directory(repo):
    assert list_files(repo.root, "*.py", 100).splitlines()[0] == '3 files matching "*.py"'
    assert "money.py" in list_files(repo.root, "app/util/", 100)
    assert list_files(repo.root, "*.java", 100) == 'No files matching "*.java".'


def test_list_files_says_what_the_limit_cut(repo):
    text = list_files(repo.root, None, 2)
    assert text.splitlines()[-1] == "[truncated: 4 more files] limit=6 shows all"


def test_read_file_numbers_lines_and_pages(repo):
    assert read_file(repo.root, "app/util/money.py", 1, 400) == "\n".join(
        [
            "app/util/money.py  lines 1-5 of 5",
            "1  RATE = 2",
            "2  ",
            "3  ",
            "4  def convert(x):",
            "5      return x * RATE",
        ]
    )
    page = read_file(repo.root, "./app/util/money.py", 2, 2).splitlines()
    assert page[0] == "app/util/money.py  lines 2-3 of 5"
    assert page[-1] == "[truncated: 2 more lines] start_line=4 continues"
    assert read_file(repo.root, "app/util/money.py", 9, 10) == (
        "app/util/money.py has 5 lines; start_line=9 is past the end."
    )


@pytest.mark.parametrize("path", ["../outside.py", "/etc/passwd", "C:/Windows/win.ini", ""])
def test_read_file_refuses_paths_outside_the_repo(repo, path):
    assert read_file(repo.root, path, 1, 10).startswith("Refusing path")


@pytest.mark.parametrize("path", [".git/config", ".env", "app/missing.py"])
def test_read_file_only_reads_files_git_would_list(repo, path):
    assert read_file(repo.root, path, 1, 10).startswith("Not a file in this repository")


def test_read_file_suggests_files_with_the_same_name(repo):
    text = read_file(repo.root, "money.py", 1, 10)
    assert text.endswith("Did you mean: app/util/money.py?")


def test_read_file_refuses_binary_files(repo):
    assert read_file(repo.root, "logo.png", 1, 10) == "logo.png is a binary file (4 bytes)."


def test_read_file_refuses_symlinks(repo, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("outside\n")
    try:
        os.symlink(secret, repo.root / "link.txt")
    except OSError:
        pytest.skip("symlinks not available")
    assert "symlink" in read_file(repo.root, "link.txt", 1, 10)


def test_read_file_cuts_long_lines_and_stops_at_its_cap(repo):
    repo.write("big.py", "\n".join("x = '" + "y" * 1000 + "'" for _ in range(400)) + "\n")
    text = read_file(repo.root, "big.py", 1, 400)
    assert estimate_tokens(text) <= BASELINE_CAPS["read_file"]
    assert max(len(line) for line in text.splitlines()) <= 310
    last = text.splitlines()[-1]
    assert last.startswith("[truncated: ") and "output cap reached; start_line=" in last


def test_grep_groups_matches_by_file(repo):
    assert grep(repo.root, r"def \w+", None, False, 50) == "\n".join(
        [
            '2 matches in 2 files for "def \\w+"',
            "app/orders.py",
            "  1: def total(items, tax=0):",
            "app/util/money.py",
            "  4: def convert(x):",
        ]
    )
    assert grep(repo.root, "RATE", "app/util", True, 50).startswith("2 matches in 1 files")
    assert grep(repo.root, "SECRET", None, True, 50) == 'No matches for "SECRET".'


def test_grep_limit_and_errors(repo):
    text = grep(repo.root, "RATE", None, True, 1)
    assert text.splitlines()[-1] == "[truncated: 1 more matches] limit=2 shows all"
    assert grep(repo.root, "(unclosed", None, False, 10).startswith("git grep failed")
    assert grep(repo.root, "", None, False, 10).startswith("Empty pattern")
    assert grep(repo.root, "x", "../..", False, 10).startswith("Refusing path")


def test_git_diff_whole_and_one_file(repo):
    whole = git_diff(repo.root, repo.base, repo.head_sha, "main...HEAD", None, 3)
    assert whole.splitlines()[0] == "diff main...HEAD"
    assert "+def total(items, tax=0):" in whole
    assert "new file mode" in whole
    one = git_diff(repo.root, repo.base, repo.head_sha, "main...HEAD", "app/new.py", 3)
    assert one.splitlines()[0] == "diff main...HEAD app/new.py"
    assert "orders.py" not in one
    none = git_diff(repo.root, repo.base, repo.head_sha, "main...HEAD", "README.md", 3)
    assert none == "No changes README.md in main...HEAD."


def test_git_diff_cut_names_the_files_not_shown(repo):
    for i in range(30):
        repo.write(f"gen/m{i:02d}.py", "".join(f"v{j} = {j}\n" for j in range(40)))
    head = repo.commit("three")
    text = git_diff(repo.root, repo.head_sha, head, "x...y", None, 3)
    assert estimate_tokens(text) <= BASELINE_CAPS["git_diff"]
    last = text.splitlines()[-1]
    assert last.startswith("[truncated: ") and "files not shown: gen/m" in last
    assert last.endswith("output cap reached; pass path= for one file")
