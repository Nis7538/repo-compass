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
