# ADR-004: Index Location, File Discovery and Incremental Updates

## Status
Accepted

## Context
The index must be cheap to keep fresh. Agents will ask questions while code changes
(M2 refreshes lazily on each tool call), so re-indexing an unchanged or slightly changed
repo has to take well under a second. The tool is also read-only with respect to the
target repository. It must never write files there.

## Decision

### Where the index lives
One SQLite file per repository in the user's cache directory:
`%LOCALAPPDATA%\repo-compass\` on Windows, `$XDG_CACHE_HOME/repo-compass/` or
`~/.cache/repo-compass/` elsewhere. The file is named `<repo-dir-name>-<sha1(abs path)[:12]>.db`.
`--db` overrides the location. Nothing is written inside the repository.

### Which files are indexed
- In a git work tree: `git ls-files --cached --others --exclude-standard`. Git applies its
  own ignore rules exactly (nested `.gitignore`, `.git/info/exclude`, global excludes).
  Untracked-but-not-ignored files are included, because agents work on uncommitted code.
- Outside git: `os.walk`. This fallback **does not read `.gitignore`**.
- Always skipped: `.java`/`.py` files under vendored or build directories (`vendor`,
  `third_party`, `node_modules`, `.venv`, `venv`, `site-packages`, `build`, `dist`,
  `target`, `.gradle`, `__pycache__`, `.tox`, …), symlinks, files over 1 MB, and files with
  a NUL byte in the first 8 KB (binary). Git submodules are not descended into.

### What counts as changed
Per file, cheapest check first:
1. Size and mtime match the index: unchanged, and the file is not read at all.
2. Otherwise, read it and compute sha256. Same hash: unchanged (only the stored mtime is updated).
3. Otherwise, re-extract, then delete the file's rows and insert new ones.

Two extra rules:
- **Racy mtime.** A file whose mtime falls within 2 seconds before the previous run
  started is always hashed. An edit that lands in the same mtime tick as an index run
  would otherwise go unnoticed. This is the same "racily clean" problem git solves for
  its own index.
- **Module renames.** A Python file whose dotted module name changes (because an
  `__init__.py` appeared or disappeared) is re-extracted even if its bytes are the same.

Files in the index that were not found this run are deleted. Their symbols, imports and
refs go with them via `ON DELETE CASCADE`. The whole run is one transaction: a crash
leaves the previous index intact.

### Why this keeps other files untouched
Every row belongs to one file, and no row stores a cross-file fact such as a resolved
call target (ADR-003 resolves at query time). So re-indexing file A never needs to
rewrite rows of file B. `tests/test_pipeline.py` asserts this: after one file is edited,
every other file's symbol, import and ref rows are identical, down to their ids.

## Alternatives Considered
- **`.compass/index.db` inside the repo.** Easy to find and to delete, but it writes into
  the target repo, which breaks the read-only guarantee and clutters `git status` for
  anyone who doesn't gitignore it.
- **Parse `.gitignore` ourselves (the `pathspec` library).** Works outside git too, but it
  is a new dependency, and subtle rule differences from git are easy to get wrong.
  Almost every repo an agent works in is a git repo.
- **Hash every file every run.** Simpler, but it reads the whole repo on every refresh.
  The size+mtime fast path makes a no-change reindex of a 200k-line repo take about 0.1s.
- **Watch the filesystem (inotify / FSEvents).** Needs a long-running process and has a
  different code path per platform. Lazy checks on demand are enough for our use.

## Consequences
- `compass index` is safe to run repeatedly and fast when little changed (see
  docs/benchmarks.md).
- Deleting an index is always safe: it is a cache and is rebuilt on the next run. A schema
  change also triggers a rebuild, so there are no migrations.
- Outside git, ignored files may be indexed. Vendored directories are still skipped by name.
- Moving a repository to another path creates a new index. The old one stays in the cache
  directory until it is deleted by hand.
