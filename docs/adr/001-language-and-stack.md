# ADR-001: Language and Stack Choice

## Status
Accepted

## Context
We need to build an MCP server that provides structural code intelligence (symbols, references, dependencies) to coding agents, plus a CLI and an eval harness. The tool must parse Java and Python source code, store an index for fast querying, and communicate over the MCP protocol (stdio transport).

## Decision
- **Language:** Python 3.12
- **Package manager:** uv (fast, lockfile-based, handles Python versions)
- **Parsing:** tree-sitter with language-specific grammars (tree-sitter-java, tree-sitter-python)
- **Storage:** SQLite with FTS5 (see ADR-002)
- **MCP SDK:** Official Python MCP SDK (FastMCP) over stdio
- **CLI:** typer
- **LLM calls (review agent + evals):** anthropic SDK
- **Lint/format:** ruff
- **Tests:** pytest
- **CI:** GitHub Actions

## Alternatives Considered

### Rust or Go
Faster runtime, but significantly slower development iteration. The MCP ecosystem has fewer mature libraries for these languages. Tree-sitter bindings exist but are less ergonomic than the Python ones. For a tool indexing repos up to ~100k LOC, Python performance is sufficient.

### TypeScript
Strong MCP SDK support (the reference implementation). However, tree-sitter bindings for TypeScript are less mature than Python's, and the eval harness involves significant data processing where Python is more natural. The anthropic SDK is available for both, but Python has a richer ecosystem for scripting and analysis tasks.

## Consequences
- Fast development cycle; the entire stack is well-documented and widely used.
- Python's performance is adequate for the target repo sizes (under ~100k LOC). If indexing speed becomes a bottleneck on larger repos, tree-sitter parsing is the hot path and it runs native code regardless.
- uv provides reproducible installs via lockfile and handles Python version management.
- Single language across the server, CLI, agent, and eval harness — no context switching.
