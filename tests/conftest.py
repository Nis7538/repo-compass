"""Shared pytest fixtures."""

import shutil

import pytest

from tests.helpers import FIXTURES


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
