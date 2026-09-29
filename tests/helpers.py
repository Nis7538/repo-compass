"""Helpers shared by tests (imported explicitly, unlike conftest fixtures)."""

import subprocess
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def init_git_repo(repo: Path) -> None:
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init", "--allow-empty")
