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
- `mcp` 2.x (official Python SDK, `MCPServer`, formerly FastMCP) over stdio
- `tree-sitter` with Java and Python grammars
- SQLite (stdlib, FTS5 for symbol search). No external services.
- `typer` for CLI, `anthropic` SDK for the review agent and eval runner
- GitHub Actions for CI

## Commands (keep these working; update here if they change)
- `uv sync` — install (`uv sync --extra agent` adds the anthropic SDK, needed only by code that calls the Claude API)
- `uv run pytest -q` — tests
- `uv run ruff check . && uv run ruff format --check .` — lint
- `uv run compass --help` — CLI
- `uv run compass index <path>` — index a repo (incremental; DB in user cache dir, `--db` to override)
- `uv run compass symbol <name> --repo <path> [--refs]` — look up symbols and their call sites
- `uv run compass serve --repo <path> [--db <file>]` — MCP server over stdio (indexes in the background)
- `uv run compass review --repo <path> --base main --head HEAD [--tools compass|baseline|both|none] [--max-cost 1.00]` — Claude review of a change (needs `--extra agent` and an API key; costs API usage; logs to the user cache dir)
- `claude mcp add repo-compass -- uv run --project <compass checkout> compass serve --repo .` — register with Claude Code
- `uv run pytest -q -m "not slow"` — tests without the 50k-line benchmark and the stdio subprocess test
- `uv run python scripts/bench_index.py <path> | --synthetic 50000` — indexing benchmark
- `uv run python scripts/calibrate_tokens.py [--repo <path>]` — token estimate vs real `count_tokens` (manual only, never in CI; needs `--extra agent` and an API key)
- `uv run python scripts/e2e_claude.py --repo <path> "question" [--builtin]` — headless Claude Code run with the compass tools (manual only, never in CI; costs API usage)

## Guardrails
- Tool outputs returned to agents must be size-capped and truncated with an explicit "truncated, N more" marker. Token efficiency is a core feature.
- Read-only: this tool never modifies the target repository.
- No proprietary or employer-related code, names, or data anywhere in this repo. Test fixtures are either written from scratch or come from permissively licensed OSS with attribution.
- Each tool has a documented token cap enforced by a test. Never add default-on bodies or long context lines to tool output.
