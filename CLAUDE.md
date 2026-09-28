# CLAUDE.md — repo-compass

You are building `repo-compass`, an open-source MCP server + CLI that gives coding agents
structural code intelligence, plus an eval harness that measures whether it actually helps.
The full plan is in PLAN.md. Follow it milestone by milestone.

## How to work
- Work on ONE milestone per session. Start in plan mode: propose the task breakdown, wait for approval, then implement.
- Small commits, one logical change each. Conventional commit messages (feat:, fix:, test:, docs:, chore:).
- Write tests alongside code. A milestone is not done until its acceptance criteria in PLAN.md pass and CI is green.
- Never commit secrets. API keys come from environment variables only. Add `.env` to .gitignore.
- Do not add dependencies without stating why in the commit message.
- When a design choice is non-obvious, write a short ADR in docs/adr/ (context, decision, alternatives, consequences).
  The owner must be able to explain every ADR in an interview, so keep them plain and specific.
- Prefer boring, readable code over clever code. The owner will read all of it.
- Be honest in docs: document limitations (e.g. name-based reference resolution is approximate) and report eval results as they are, including unflattering ones.

## Stack
- Python 3.12, `uv` for env and deps, `ruff` for lint/format, `pytest` for tests
- `mcp` (official Python SDK, FastMCP) over stdio
- `tree-sitter` with Java and Python grammars
- SQLite (stdlib, FTS5 for symbol search). No external services.
- `typer` for CLI, `anthropic` SDK for the review agent and eval runner
- GitHub Actions for CI

## Commands (keep these working; update here if they change)
- `uv sync` — install
- `uv run pytest -q` — tests
- `uv run ruff check . && uv run ruff format --check .` — lint
- `uv run compass --help` — CLI

## Guardrails
- Tool outputs returned to agents must be size-capped and truncated with an explicit "truncated, N more" marker. Token efficiency is a core feature.
- Read-only: this tool never modifies the target repository.
- No proprietary or employer-related code, names, or data anywhere in this repo. Test fixtures are either written from scratch or come from permissively licensed OSS with attribution.
