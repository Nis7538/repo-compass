# repo-compass

An MCP server that gives coding agents structural understanding of a codebase — symbols, references, module dependencies, diff impact, hotspots — plus an eval harness that measures whether it actually helps.

> Under construction. See [PLAN.md](PLAN.md) for the roadmap. Done so far: M0 (scaffold), M1 (indexer), M2 (MCP server), M3 (git-aware tools).

## Install

```bash
uv sync                 # add --extra agent for the anthropic SDK (API scripts only)
```

## Usage

```bash
uv run compass index path/to/repo              # index Java + Python (incremental on re-run)
uv run compass symbol Order.add --repo path/to/repo --refs
```

`compass symbol` accepts a simple name (`add`), a qualified suffix (`Order.add`) or search
words (`getUser` also finds `get_user_by_id`). `--refs` lists call sites, each labeled with
a confidence tier:

```
method com.example.shop.model.Order.add  public Order add(Item item)  .../model/Order.java:25
    exact    .../model/Order.java:36:13  add(...)
    likely   .../service/OrderService.java:17:19  order.add(...)
```

The index is stored in your user cache directory (never inside the repository); `--db`
overrides the location.

## Use it from Claude Code

From the repository you want to work on, register the server (replace the path with
your repo-compass checkout):

```bash
claude mcp add repo-compass -- uv run --project /path/to/repo-compass compass serve --repo .
```

`--project` (not `--directory`) keeps the working directory where Claude Code started, so
`--repo .` is your repository. There is no separate indexing step: the server indexes in
the background when it starts and refreshes before every tool call. The first call on a
large repo may reply "index building, retry" if the build takes more than a few seconds.

Eight read-only tools: `repo_summary`, `search_symbols`, `get_symbol`, `find_references`,
`file_outline`, `module_dependencies`, and two that read git history: `diff_impact` (changed
symbols between two refs or against the working tree, their callers outside the diff,
dangling uses of removed ones) and `hotspots` (files ranked by commits x lines inside
methods). Answers are compact text, ranked, and capped per
tool with an explicit `[truncated: N more]` line. Formats, ranking rules and token caps:
[docs/tools.md](docs/tools.md). An end-to-end run in Claude Code on commons-lang and flask,
including what went wrong: [docs/e2e-m2.md](docs/e2e-m2.md); for the git tools,
[docs/e2e-m3.md](docs/e2e-m3.md).

## Limitations

- **Reference resolution is approximate.** It uses names, imports, packages and scopes, not
  types. `exact` means the binding is certain from syntax; `likely` means the target's class is
  visible but the receiver's type is unknown; `possible` means only the name matches. There is
  no type inference or override/dispatch analysis. Details and known failure cases:
  [ADR-003](docs/adr/003-reference-resolution.md).
- Only call sites are references. Type usages (parameters, `extends`, casts) are not.
- `module_dependencies` sees import statements only. Java classes used from the same
  package, or written fully qualified without an import, add no edge.
- Tool token counts are estimated as characters / 3 (no offline Claude tokenizer), see
  [ADR-005](docs/adr/005-tool-output-format.md).
- Outside a git work tree, `.gitignore` is not applied ([ADR-004](docs/adr/004-index-storage-and-incremental-updates.md)).
- `diff_impact` finds changed symbols exactly (both versions are parsed), but their callers
  come from the same approximate resolution, from the working tree's index, with no
  override analysis. `hotspots` counts commits per file, not lines changed. Both need git.
  Limits: [docs/tools.md](docs/tools.md), [ADR-007](docs/adr/007-diff-impact-and-hotspots.md).
- Java and Python only.

Indexing speed: about 2.4s for a 50k-line repo, and 7.3s for apache/commons-lang (207k lines).
See [docs/benchmarks.md](docs/benchmarks.md), including the slower first run after a fresh clone.

## Development

```bash
uv run pytest -q                 # tests (includes a benchmark and a stdio test marked slow)
uv run pytest -q -m "not slow"   # skip the slow ones
uv run ruff check .              # lint
uv run ruff format --check .     # format check
uv run python scripts/bench_index.py --synthetic 50000
```

Design decisions are recorded in [docs/adr/](docs/adr/).

## License

MIT
