# Indexing benchmarks

M1 acceptance criterion: a ~50k LOC repository indexes cold in under 15 seconds.

## How to reproduce

```bash
uv run python scripts/bench_index.py --synthetic 50000   # generated Java+Python corpus
uv run python scripts/bench_index.py /path/to/checkout   # any repo, read-only
```

Each run indexes into a fresh temporary database ("cold": empty index), then indexes
again with no changes ("no-change reindex"). Three runs; the median is reported.
"Lines" counts every line of the indexed .java/.py files, including comments and blanks.
The target repository is never modified.

## Results (2026-09-29)

Machine: Windows 11, AMD Ryzen (Family 23 Model 96), Python 3.12.14, SQLite 3.53.

| Repo | Files | Lines | Symbols | Call sites | Cold (median) | No-change reindex |
|------|------:|------:|--------:|-----------:|--------------:|------------------:|
| synthetic (Java+Python) | 281 | 50,355 | 5,130 | 28,350 | 2.42s | 0.07s |
| [apache/commons-lang](https://github.com/apache/commons-lang) @ 29624cd (Java, Apache-2.0) | 629 | 207,607 | 14,521 | 87,571 | 7.35s | 0.12s |
| [pallets/flask](https://github.com/pallets/flask) @ d73fa1c (Python, BSD-3-Clause) | 83 | 18,345 | 1,622 | 3,906 | 0.62s | 0.06s |

Caveats, stated plainly:
- **The first run after cloning is slower.** On commons-lang the first run took 16.6s
  versus 7.3s for the next two. The OS file cache was cold, and on Windows the antivirus
  scans newly written files on first read. A real first `compass index` after a fresh
  clone will look like that first run. That is still under the budget per 50k lines
  (about 4s per 50k lines).
- One commons-lang file (`ClassUtilsOssFuzzTest.java`) contains NUL bytes and was skipped
  as binary. None of the other 628 files had tree-sitter syntax errors.
- The synthetic corpus is generated code (see `tests/perf_corpus.py`). It stands in for
  real code only in parse and extraction cost. The CI test `tests/test_perf.py` indexes it
  and asserts < 15s. CI runners are slower than this machine, so CI timings will be higher.
- The source repos were cloned into a scratch directory to measure them and are not
  vendored into this repository.

## Where the time goes

A cProfile of the synthetic cold run (4.1s under the profiler):
- about 55%: tree-sitter parsing (0.5s, native code) plus walking the tree in Python (1.7s)
- about 20%: opening and reading files (inflated on Windows by antivirus scanning of
  freshly written files)
- about 20%: SQLite inserts, including the FTS triggers

Nothing dominates, so no optimization was done for M1. If larger repos need it, the first
step is a process pool for parse and extract (it is CPU-bound, and the per-file work is
independent). The next is batching symbol inserts.
