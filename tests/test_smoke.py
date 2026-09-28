"""Smoke tests: package imports and CLI basics."""

from typer.testing import CliRunner

from compass import __version__
from compass.cli import app

runner = CliRunner()


def test_version_string():
    assert __version__ == "0.1.0"


def test_cli_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "compass" in result.output.lower()


def test_cli_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output
