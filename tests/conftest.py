"""Shared pytest fixtures."""

import os
import shutil

import pytest

from tests.helpers import FIXTURES


def _hide_git_config(mp: pytest.MonkeyPatch) -> None:
    mp.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    mp.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch):
    """Keep the machine's git config out of every test: no user or system config file.

    Without this a developer's global settings (autocrlf, a pager, fsmonitor, signing)
    would change what both the test helpers and the code under test see.
    """
    _hide_git_config(monkeypatch)


@pytest.fixture(scope="session")
def shop(tmp_path_factory):
    """tests/shop_history.py built once, feature checked out, and its index. Read-only."""
    from compass.indexer.pipeline import index_repo
    from tests.gitrepo import ScriptedRepo
    from tests.shop_history import shop_history

    with pytest.MonkeyPatch.context() as mp:  # the autouse fixture is per test
        _hide_git_config(mp)
        tmp = tmp_path_factory.mktemp("shop")
        repo = shop_history(ScriptedRepo(tmp / "repo"))
        index_repo(repo.root, tmp / "index.db")
    return repo, tmp / "index.db"


@pytest.fixture
def copy_fixture(tmp_path):
    """Copy fixtures/<name> into a temp dir so a test can modify it freely."""

    def _copy(name: str):
        dest = tmp_path / "repo"
        shutil.copytree(FIXTURES / name, dest)
        return dest

    return _copy


@pytest.fixture(scope="session")
def fixtures_db(tmp_path_factory):
    """An index of fixtures/ (Java + Python), built once per test session. Read-only."""
    from compass.indexer.pipeline import index_repo

    db = tmp_path_factory.mktemp("fixtures-index") / "index.db"
    index_repo(FIXTURES, db)
    return db


@pytest.fixture
def fx_conn(fixtures_db):
    from compass.store.db import open_index

    conn = open_index(fixtures_db)
    yield conn
    conn.close()
