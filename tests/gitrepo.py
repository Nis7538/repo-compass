"""A git repository built step by step by a test, with the same commit SHAs everywhere.

A commit's SHA depends only on its tree, parents, author, committer, dates and
message. ScriptedRepo fixes all of them: file content is written byte for byte with
'\\n' line endings, the author is fixed, and every commit passes an explicit date.
So the same script produces the same SHAs on every machine and every run, and tests
can assert whole outputs, short SHAs included, as literal strings.

The machine's own git config is kept out by the autouse `isolated_git` fixture in
tests/conftest.py (GIT_CONFIG_GLOBAL / GIT_CONFIG_NOSYSTEM), which also covers the
code under test.
"""

import os
import subprocess
from pathlib import Path

AUTHOR = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.com",
}
DEFAULT_DATE = "2026-02-03T10:00:00+00:00"


class ScriptedRepo:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str, date: str = DEFAULT_DATE) -> str:
        env = {**os.environ, **AUTHOR, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        proc = subprocess.run(
            ["git", *args], cwd=self.root, env=env, capture_output=True, check=False
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace')}")
        return proc.stdout.decode("utf-8").strip()

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))  # '\n' stays '\n' on Windows too

    def remove(self, path: str) -> None:
        (self.root / path).unlink()

    def rename(self, old: str, new: str) -> None:
        target = self.root / new
        target.parent.mkdir(parents=True, exist_ok=True)
        (self.root / old).rename(target)

    def commit(self, message: str, when: str = DEFAULT_DATE) -> str:
        """Stage everything and commit it. Returns the full SHA."""
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message, date=when)
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def branch(self, name: str) -> None:
        self.git("branch", name)

    def checkout(self, name: str) -> None:
        self.git("checkout", "-q", name)

    def merge(self, name: str, message: str, when: str = DEFAULT_DATE) -> str:
        """A real merge commit (never a fast-forward). Returns its SHA."""
        self.git("merge", "-q", "--no-ff", "-m", message, name, date=when)
        return self.head()
