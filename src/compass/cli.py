"""CLI entry point for repo-compass."""

import os
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
DEFAULT_MODEL = "claude-opus-5-5"
# The anthropic SDK refuses non-streaming requests that could run past 10 minutes,
# which it estimates from max_tokens (about 21,000 and up).
MAX_RESPONSE_TOKENS = 21000


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


@app.command()
def serve(
    repo: Path = typer.Option(Path("."), "--repo", help="Repository to serve."),
    db: Path | None = DB_OPTION,
) -> None:
    """Run the MCP server over stdio for one repository (indexes it in the background)."""
    if not repo.is_dir():
        raise typer.BadParameter(f"not a directory: {repo}")
    from compass.server import build_server  # the MCP SDK is only needed here

    build_server(repo, db).run("stdio")


def make_client():
    """The Claude API client. Tests replace this function with a scripted fake."""
    try:
        import anthropic
    except ImportError:
        typer.echo(
            "compass review calls the Claude API and needs the optional anthropic SDK:"
            " run `uv sync --extra agent`.",
            err=True,
        )
        raise typer.Exit(2) from None
    return anthropic.Anthropic()  # credentials from the environment, never from here


@app.command()
def review(
    base: str = typer.Option("main", "--base", help="Branch or commit the change is based on."),
    head: str = typer.Option("HEAD", "--head", help="The change; must be checked out."),
    repo: Path = typer.Option(Path("."), "--repo", help="Repository to review."),
    tools: str = typer.Option(
        "compass", "--tools", help="Tools the agent gets: compass, baseline, both or none."
    ),
    model: str | None = typer.Option(
        None, "--model", help=f"Claude model (default: $COMPASS_MODEL, else {DEFAULT_MODEL})."
    ),
    max_turns: int = typer.Option(20, "--max-turns", min=1, help="API requests, at most."),
    max_tokens: int = typer.Option(
        16000,
        "--max-tokens",
        min=1024,
        max=MAX_RESPONSE_TOKENS,
        help="Output tokens per response (thinking included).",
    ),
    max_cost: float = typer.Option(
        1.00, "--max-cost", min=0.01, help="Hard cap on the run's cost in US dollars."
    ),
    out: Path | None = typer.Option(None, "--out", help="Write the review here, not stdout."),
    log: Path | None = typer.Option(None, "--log", help="Run log (default: user cache dir)."),
    transcript: Path | None = typer.Option(
        None, "--transcript", help="Transcript file (default: user cache dir)."
    ),
    no_transcript: bool = typer.Option(False, "--no-transcript", help="Write no transcript."),
    db: Path | None = DB_OPTION,
) -> None:
    """Review the change base...head with Claude and print a Markdown review."""
    from compass.agent.loop import Limits
    from compass.agent.pricing import UnknownModelError
    from compass.agent.review import ReviewError, run_review
    from compass.agent.runlog import inside
    from compass.agent.toolsets import MODES

    if tools not in MODES:
        raise typer.BadParameter(f"choose one of {', '.join(MODES)}", param_hint="--tools")
    if not repo.is_dir():
        raise typer.BadParameter(f"not a directory: {repo}", param_hint="--repo")
    if out is not None and inside(out, repo):
        typer.echo(f"Refusing to write the review inside the repository: {out}", err=True)
        raise typer.Exit(2)
    model = model or os.environ.get("COMPASS_MODEL") or DEFAULT_MODEL
    limits = Limits(max_turns=max_turns, max_tokens=max_tokens, max_cost_usd=max_cost)
    client = make_client()
    try:
        run = run_review(
            client,
            repo,
            base,
            head,
            tools,
            model,
            limits,
            db_path=db,
            log_path=log,
            transcript_path=transcript,
            write_transcript=not no_transcript,
        )
    except (ReviewError, UnknownModelError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None

    record = run.record
    if record["dirty"]:
        typer.echo(
            "warning: the working tree has uncommitted changes; the tools saw them, the diff"
            " did not.",
            err=True,
        )
    if out is not None:
        out.write_text(run.result.text + "\n", encoding="utf-8")
    elif run.result.text:
        typer.echo(run.result.text)
    calls = ", ".join(f"{name} {n}" for name, n in record["tool_calls"].items()) or "none"
    prompt = (
        record["input_tokens"]
        + record["cache_creation_input_tokens"]
        + record["cache_read_input_tokens"]
    )
    typer.echo(
        f"[{tools}] {record['turns']} turns; tool calls: {calls}; tokens in {prompt}"
        f" (cache read {record['cache_read_input_tokens']}) out {record['output_tokens']};"
        f" ${record['cost_usd']:.3f} of ${max_cost:.2f}; stop: {record['stop_reason']}"
        + (" after a wrap-up" if record["wrap_up"] else ""),
        err=True,
    )
    if record["error"]:
        typer.echo(f"error: {record['error']}", err=True)
    if not run.result.text or record["stop_reason"] == "error":
        raise typer.Exit(1)


def format_symbol(sym: SymbolRow) -> str:
    return f"{sym.kind} {sym.qualified_name}  {sym.signature}  {sym.path}:{sym.start_line}"


def format_reference(reference: Reference) -> str:
    ref = reference.ref
    call = f"{ref.receiver}.{ref.name}" if ref.receiver else ref.name
    return f"{reference.confidence:<8} {ref.path}:{ref.line}:{ref.col}  {call}(...)"


if __name__ == "__main__":
    app()
