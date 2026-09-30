"""IndexManager: bounded waits, single flight, stale answers, failures and retries.

A fake index function blocks on a threading.Event, so whether a refresh has
finished is decided by the test, not by timing.
"""

import threading

from compass.index_manager import IndexManager, index_is_ready
from compass.indexer.pipeline import index_repo
from tests.helpers import FIXTURES


class FakeIndexer:
    def __init__(self, fail_times: int = 0):
        self.calls = 0
        self.release = threading.Event()
        self.fail_times = fail_times
        self.entered = threading.Semaphore(0)

    def __call__(self, root, db):
        self.calls += 1
        self.entered.release()
        self.release.wait(10)
        if self.fail_times:
            self.fail_times -= 1
            raise OSError("disk on fire")


def _manager(tmp_path, fake, ready=False, budget=0.05):
    db = tmp_path / "i.db"
    if ready:
        index_repo(FIXTURES / "python", db)
    return IndexManager(FIXTURES / "python", db, fake, ready_budget=budget, cold_budget=budget)


def test_fresh_when_refresh_finishes_within_budget(tmp_path):
    fake = FakeIndexer()
    fake.release.set()
    manager = _manager(tmp_path, fake, ready=True, budget=5)
    freshness = manager.ensure_fresh()
    assert freshness.ready
    assert freshness.status is None
    assert freshness.summary == "fresh"


def test_stale_answer_when_refresh_is_slow(tmp_path):
    fake = FakeIndexer()
    manager = _manager(tmp_path, fake, ready=True)
    freshness = manager.ensure_fresh()
    assert freshness.ready
    assert freshness.status.startswith("[index: refresh running")
    assert "may be missing" in freshness.status
    fake.release.set()


def test_not_ready_while_first_build_runs(tmp_path):
    fake = FakeIndexer()
    manager = _manager(tmp_path, fake)
    freshness = manager.ensure_fresh()
    assert not freshness.ready
    assert freshness.status.startswith("[index: building, ")
    assert "Retry this call in a few seconds." in freshness.status
    fake.release.set()


def test_concurrent_calls_share_one_refresh(tmp_path):
    fake = FakeIndexer()
    manager = _manager(tmp_path, fake, ready=True)
    manager.start()
    fake.entered.acquire(timeout=5)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(manager.ensure_fresh())) for _ in range(5)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert fake.calls == 1
    assert all(r.status.startswith("[index: refresh running") for r in results)
    fake.release.set()


def test_next_call_after_a_finished_refresh_starts_a_new_one(tmp_path):
    fake = FakeIndexer()
    fake.release.set()
    manager = _manager(tmp_path, fake, ready=True, budget=5)
    manager.ensure_fresh()
    manager.ensure_fresh()
    assert fake.calls == 2


def test_failure_with_old_index_answers_and_says_so(tmp_path):
    fake = FakeIndexer(fail_times=1)
    fake.release.set()
    manager = _manager(tmp_path, fake, ready=True, budget=5)
    freshness = manager.ensure_fresh()
    assert freshness.ready
    assert freshness.status.startswith("[index: refresh failed: OSError: disk on fire;")
    assert manager.ensure_fresh().status is None  # retried and recovered


def test_failure_without_index_is_actionable(tmp_path):
    fake = FakeIndexer(fail_times=1)
    fake.release.set()
    manager = _manager(tmp_path, fake, budget=5)
    freshness = manager.ensure_fresh()
    assert not freshness.ready
    assert freshness.status.startswith("[index: build failed: OSError: disk on fire.")
    assert "--repo" in freshness.status


def test_ready_requires_a_committed_first_build(tmp_path):
    from compass.store.db import connect

    db = tmp_path / "i.db"
    assert not index_is_ready(db)
    connect(db).close()  # schema only, as during the first build
    assert not index_is_ready(db)
    index_repo(FIXTURES / "python", db)
    assert index_is_ready(db)
