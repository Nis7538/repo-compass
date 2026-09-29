"""CLI entry point for repo-compass."""

from pathlib import Path

import typer

from compass import __version__
from compass.indexer.pipeline import index_repo
from compass.paths import default_db_path
from compass.store.db import IndexNotFoundError, open_index
from compass.store.queries import SymbolRow, find_symbols
from compass.store.resolve import Reference, Resolver

app = typer.Typer(
    name="compass",
    help="Structural code intelligence for coding agents.",
    no_args_is_help=True,
    add_completion=False,
)

DB_OPTION = typer.Option(None, "--db", help="Index file (default: per-repo file in user cache).")


@app.command()
def version() -> None:
    """Print the current version."""
    typer.echo(f"repo-compass {__version__}")


@app.command()
def info() -> None:
    """Show project information."""
    typer.echo(f"repo-compass {__version__}")
    typer.echo("An MCP server for structural code intelligence.")


@app.command()
def index(
    path: Path = typer.Argument(Path("."), help="Repository to index."),
    db: Path | None = DB_OPTION,
) -> None:
    """Index (or incrementally re-index) the Java and Python files in a repository."""
    if not path.is_dir():
        raise typer.BadParameter(f"not a directory: {path}")
    result = index_repo(path, db)
    conn = open_index(result.db_path)
    try:
        symbols = conn.execute("SELECT count(*) FROM symbols").fetchone()[0]
        refs = conn.execute("SELECT count(*) FROM refs").fetchone()[0]
    finally:
        conn.close()
    typer.echo(f"Indexed {result.root} ({result.discovery}) in {result.elapsed_s:.2f}s")
    typer.echo(
        f"  files: {result.files} found, {len(result.parsed)} parsed, "
        f"{result.unchanged} unchanged, {len(result.removed)} removed, "
        f"{len(result.skipped)} skipped"
    )
    typer.echo(f"  index: {symbols} symbols, {refs} call sites -> {result.db_path}")


@app.command()
def symbol(
    name: str = typer.Argument(..., help="Name, 'Class.method' suffix, or search words."),
    repo: Path = typer.Option(Path("."), "--repo", help="Repository the index belongs to."),
    db: Path | None = DB_OPTION,
    kind: str | None = typer.Option(None, "--kind", help="Only this kind, e.g. method, class."),
    limit: int = typer.Option(20, "--limit", min=1, help="Maximum symbols to show."),
    refs: bool = typer.Option(False, "--refs", help="Also show call sites of each symbol."),
    ref_limit: int = typer.Option(10, "--ref-limit", min=1, help="Maximum call sites each."),
) -> None:
    """Find symbols by name and optionally list their call sites."""
    db_path = db or default_db_path(repo)
    try:
        conn = open_index(db_path)
    except IndexNotFoundError:
        typer.echo(f"No index for {repo.resolve()}. Run: compass index {repo}", err=True)
        raise typer.Exit(1) from None
    try:
        matches = find_symbols(conn, name, kind)
        if not matches:
            typer.echo(f"No symbols match '{name}'.")
            return
        resolver = Resolver(conn) if refs else None
        for sym in matches[:limit]:
            typer.echo(format_symbol(sym))
            if resolver is not None:
                found = resolver.find_references(sym)
                for reference in found[:ref_limit]:
                    typer.echo("    " + format_reference(reference))
                if not found:
                    typer.echo("    (no call sites found)")
                elif len(found) > ref_limit:
                    typer.echo(f"    ... truncated, {len(found) - ref_limit} more")
        if len(matches) > limit:
            typer.echo(f"... truncated, {len(matches) - limit} more")
    finally:
        conn.close()


def format_symbol(sym: SymbolRow) -> str:
    return f"{sym.kind} {sym.qualified_name}  {sym.signature}  {sym.path}:{sym.start_line}"


def format_reference(reference: Reference) -> str:
    ref = reference.ref
    call = f"{ref.receiver}.{ref.name}" if ref.receiver else ref.name
    return f"{reference.confidence:<8} {ref.path}:{ref.line}:{ref.col}  {call}(...)"
