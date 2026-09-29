"""Benchmark indexing speed on a repository (read-only) or on the synthetic corpus.

    uv run python scripts/bench_index.py PATH [--runs 3]
    uv run python scripts/bench_index.py --synthetic 50000

For each run the index goes to a fresh temporary database (cold), then the same
repository is indexed again with no changes (warm). The target repository is
never modified. Prints a Markdown table row suitable for docs/benchmarks.md.
"""

import argparse
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # for tests.perf_corpus

from compass.indexer.pipeline import index_repo  # noqa: E402


def _counts(db: Path) -> tuple[int, int]:
    conn = sqlite3.connect(db)
    try:
        symbols = conn.execute("SELECT count(*) FROM symbols").fetchone()[0]
        refs = conn.execute("SELECT count(*) FROM refs").fetchone()[0]
    finally:
        conn.close()
    return symbols, refs


def _commit(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else "-"


def bench(repo: Path, runs: int) -> None:
    cold, warm = [], []
    for _ in range(runs):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bench.db"
            first = index_repo(repo, db)
            second = index_repo(repo, db)
            cold.append(first.elapsed_s)
            warm.append(second.elapsed_s)
            symbols, refs = _counts(db)
            by_language: dict[str, int] = {}
            for path in first.parsed:
                language = "java" if path.endswith(".java") else "python"
                by_language[language] = by_language.get(language, 0) + 1

    print(f"repo: {repo} @ {_commit(repo)}")
    print(f"machine: {platform.platform()}, {platform.processor() or platform.machine()}")
    print(f"python: {platform.python_version()}")
    print(f"files: {first.files} ({by_language}), lines: {first.lines}")
    print(f"symbols: {symbols}, call sites: {refs}")
    print(f"cold: median {statistics.median(cold):.2f}s (runs: {[round(c, 2) for c in cold]})")
    print(f"no-change reindex: median {statistics.median(warm):.2f}s")
    print()
    print(
        f"| {repo.name} | {first.files} | {first.lines:,} | {symbols:,} | {refs:,} "
        f"| {statistics.median(cold):.2f}s | {statistics.median(warm):.2f}s |"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path)
    parser.add_argument("--synthetic", type=int, metavar="LINES")
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    if args.synthetic:
        from tests.perf_corpus import generate

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "synthetic"
            generate(root, args.synthetic)
            bench(root, args.runs)
    elif args.path:
        bench(args.path.resolve(), args.runs)
    else:
        parser.error("give a PATH or --synthetic LINES")


if __name__ == "__main__":
    main()
