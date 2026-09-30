"""A repository whose git config tries to run programs cannot make compass run them.

.git/config (and .gitattributes) can name commands git runs by itself: an fsmonitor
hook, an external diff, a textconv driver, a clean filter, gpg for signatures, a
pager. Each is set here to a Python command (sys.executable, so the test works on
Windows and Linux) that creates a marker file. Indexing and both git tools must
leave no marker.

The positive control runs plain git commands, without compass's flags, and checks
that every marker does appear, so the test cannot pass just because the hooks were
broken. The pager is the exception: git only starts one when stdout is a terminal,
which a test cannot provide, so it is configured but has no positive control.
"""

import subprocess
import sys
from pathlib import Path

from compass.indexer.pipeline import index_repo
from compass.store.db import open_index
from compass.tools.hotspots import hotspots
from compass.tools.impact import diff_impact
from tests.gitrepo import ScriptedRepo

HOOKS = ["fsmonitor", "external", "textconv", "clean", "gpg", "pager"]


def _command(markers: Path, name: str) -> str:
    python = Path(sys.executable).as_posix()
    marker = (markers / name).as_posix()
    return f'"{python}" -c "import pathlib; pathlib.Path(\'{marker}\').touch()"'


def _program(markers: Path, name: str) -> str:
    """An executable script, for settings git runs directly rather than through a shell
    (gpg.program). Git for Windows honors the #! line as well."""
    script = markers.parent / f"fake-{name}"
    marker = (markers / name).as_posix()
    python = Path(sys.executable).as_posix()
    script.write_text(f"#!{python}\nimport pathlib\npathlib.Path('{marker}').touch()\n")
    script.chmod(0o755)
    return script.as_posix()


def _hostile_repo(tmp_path: Path) -> tuple[ScriptedRepo, Path]:
    markers = tmp_path / "markers"
    markers.mkdir()
    repo = ScriptedRepo(tmp_path / "repo")
    repo.write(".gitattributes", "*.py diff=evil filter=evil\n")
    repo.write("app.py", "def f():\n    return 1\n")
    repo.commit("one", "2026-01-05T10:00:00+00:00")
    repo.write("app.py", "def f():\n    return 2\n")
    repo.commit("two", "2026-01-12T10:00:00+00:00")
    _sign_head(repo)
    repo.write("app.py", "def f():\n    return 3\n")  # uncommitted: the working tree differs

    # Set last: ScriptedRepo's own `git add` would otherwise run the clean filter.
    config = {
        "core.fsmonitor": _command(markers, "fsmonitor"),
        "diff.external": _command(markers, "external"),
        "diff.evil.textconv": _command(markers, "textconv"),
        "filter.evil.clean": _command(markers, "clean"),
        "gpg.program": _program(markers, "gpg"),
        "log.showSignature": "true",
        "core.pager": _command(markers, "pager"),
        "pager.log": "true",
    }
    for key, value in config.items():
        repo.git("config", key, value)
    return repo, markers


def _sign_head(repo: ScriptedRepo) -> None:
    """Replace HEAD with a copy carrying a (fake) signature, so `git log` would call gpg."""
    commit = repo.git("cat-file", "commit", "HEAD")
    header, _, message = commit.partition("\n\n")
    signature = "gpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n -----END PGP SIGNATURE-----"
    signed = f"{header}\n{signature}\n\n{message}\n"
    sha = (
        subprocess.run(
            ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
            cwd=repo.root,
            input=signed.encode(),
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    repo.git("update-ref", "refs/heads/main", sha)


def _plain_git(repo: ScriptedRepo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo.root, capture_output=True, check=False)


def test_hostile_git_config_runs_nothing(tmp_path):
    repo, markers = _hostile_repo(tmp_path)

    db = tmp_path / "index.db"
    index_repo(repo.root, db)
    conn = open_index(db)
    try:
        worktree = diff_impact(conn, "HEAD~1", None, 10)
        commits = diff_impact(conn, "HEAD~1", "HEAD", 10)
        spots = hotspots(conn, "2000-01-01", 10, False)
    finally:
        conn.close()
    assert sorted(p.name for p in markers.iterdir()) == []
    # The tools did real work, not an early error.
    assert worktree.startswith("diff HEAD~1...working tree")
    assert "body function f" in worktree
    assert commits.startswith("diff HEAD~1...HEAD")
    assert spots.startswith("hotspots since 2000-01-01: 2 commits")

    # Positive control: git without compass's precautions does run every hook.
    _plain_git(repo, "status")  # fsmonitor
    _plain_git(repo, "diff")  # external diff; the clean filter reads the working tree
    _plain_git(repo, "diff", "--no-ext-diff", "HEAD~1", "HEAD")  # textconv
    _plain_git(repo, "log", "-1")  # gpg, for the signature
    fired = sorted(p.name for p in markers.iterdir())
    assert fired == sorted(set(HOOKS) - {"pager"})
