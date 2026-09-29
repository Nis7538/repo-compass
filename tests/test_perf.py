"""M1 acceptance: a ~50k line repository indexes cold in under 15 seconds.

Marked slow (a few seconds). Deselect locally with `-m "not slow"`; CI runs it.
"""

import os

import pytest

from compass.indexer.pipeline import index_repo
from tests.perf_corpus import generate

COLD_BUDGET_S = 15.0


@pytest.mark.slow
def test_cold_index_of_50k_lines_is_under_budget(tmp_path):
    repo = tmp_path / "corpus"
    lines = generate(repo, target_lines=50_000)
    assert lines >= 50_000

    cold = index_repo(repo, tmp_path / "index.db")
    print(f"\ncold index: {len(cold.parsed)} files, {cold.lines} lines, {cold.elapsed_s:.2f}s")
    assert cold.lines >= 50_000
    assert cold.elapsed_s < COLD_BUDGET_S

    # And the incremental path: one edited file is the only one parsed.
    victim = repo / "python" / "synthetic" / "pkg3" / "mod3.py"
    victim.write_text(victim.read_text() + "\n# edited\n")
    warm = index_repo(repo, tmp_path / "index.db")
    print(f"re-index after one edit: {warm.elapsed_s:.2f}s")
    assert [p.replace(os.sep, "/") for p in warm.parsed] == ["python/synthetic/pkg3/mod3.py"]
