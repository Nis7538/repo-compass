"""CLI entry point for repo-compass."""

import typer

from compass import __version__

app = typer.Typer(
    name="compass",
    help="Structural code intelligence for coding agents.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def version() -> None:
    """Print the current version."""
    typer.echo(f"repo-compass {__version__}")


@app.command()
def info() -> None:
    """Show project information."""
    typer.echo(f"repo-compass {__version__}")
    typer.echo("An MCP server for structural code intelligence.")
