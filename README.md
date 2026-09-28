# repo-compass

An MCP server that gives coding agents structural understanding of a codebase — symbols, references, module dependencies, diff impact, hotspots — plus an eval harness that measures whether it actually helps.

> Under construction. See [PLAN.md](PLAN.md) for the roadmap.

## Install

```bash
uv sync
```

## Usage

```bash
uv run compass --help
```

## Development

```bash
uv run pytest -q              # tests
uv run ruff check .           # lint
uv run ruff format --check .  # format check
```

## License

MIT
