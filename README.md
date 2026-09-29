# repo-compass

An MCP server that gives coding agents structural understanding of a codebase — symbols, references, module dependencies, diff impact, hotspots — plus an eval harness that measures whether it actually helps.

> Under construction. See [PLAN.md](PLAN.md) for the roadmap. Done so far: M0 (scaffold), M1 (indexer).

## Install

```bash
uv sync
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

## Limitations

- **Reference resolution is approximate.** It uses names, imports, packages and scopes, not
  types. `exact` means the binding is certain from syntax; `likely` means the target's class is
  visible but the receiver's type is unknown; `possible` means only the name matches. There is
  no type inference or override/dispatch analysis. Details and known failure cases:
  [ADR-003](docs/adr/003-reference-resolution.md).
- Only call sites are references. Type usages (parameters, `extends`, casts) are not.
- Outside a git work tree, `.gitignore` is not applied ([ADR-004](docs/adr/004-index-storage-and-incremental-updates.md)).
- Java and Python only.

Indexing speed: about 2.4s for a 50k-line repo, and 7.3s for apache/commons-lang (207k lines).
See [docs/benchmarks.md](docs/benchmarks.md), including the slower first run after a fresh clone.

## Development

```bash
uv run pytest -q                 # tests (includes a ~5s benchmark marked slow)
uv run pytest -q -m "not slow"   # skip the benchmark
uv run ruff check .              # lint
uv run ruff format --check .     # format check
uv run python scripts/bench_index.py --synthetic 50000
```

Design decisions are recorded in [docs/adr/](docs/adr/).

## License

MIT
