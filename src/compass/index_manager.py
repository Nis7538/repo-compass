"""Keep the index fresh for a long-running server without blocking tool calls.

Every tool call asks ensure_fresh() first. That starts an incremental refresh in
a background thread (or joins the one already running) and waits for it for a
bounded time:

- an index exists: wait up to READY_BUDGET_S. If the refresh finished, the answer is
  fresh. If not, the call answers from the last committed index and says so in a
  status line. SQLite WAL lets readers run while the refresh writes, and the
  refresh is one transaction, so readers never see a half-written index.
- no index yet: wait up to COLD_BUDGET_S, then tell the agent the build is
  running and to retry.

A no-change refresh only stats files (0.07-0.12s on 50k-200k lines, see
docs/benchmarks.md), so there is no throttle: an edit made a second ago is seen.
Only one refresh runs at a time (single flight); concurrent calls share it.
See docs/adr/006-index-refresh-in-the-server.md.

This module has no MCP imports; tests drive it with a fake index function.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from compass.indexer.pipeline import index_repo
from compass.store.db import IndexNotFoundError, get_meta, open_index
from compass.tools.render import STATUS_WIDTH, clip

READY_BUDGET_S = 1.5
COLD_BUDGET_S = 5.0


@dataclass(frozen=True)
class Freshness:
    ready: bool  # an index exists and can be queried
    status: str | None  # '[index: ...]' line to show the agent; None when fresh
    summary: str  # short phrase for repo_summary: 'fresh', 'refresh running 4s', ...


@dataclass
class _Run:
    started: float
    done: threading.Event = field(default_factory=threading.Event)
    error: str | None = None


class IndexManager:
    def __init__(
        self,
        root: Path,
        db_path: Path,
        index_fn: Callable[[Path, Path], object] = index_repo,
        clock: Callable[[], float] = time.monotonic,
        ready_budget: float = READY_BUDGET_S,
        cold_budget: float = COLD_BUDGET_S,
    ):
        self.root = root
        self.db_path = db_path
        self._index_fn = index_fn
        self._clock = clock
        self._ready_budget = ready_budget
        self._cold_budget = cold_budget
        self._lock = threading.Lock()
        self._running: _Run | None = None
        self._ready = index_is_ready(db_path)
        self._last_ok: float | None = None

    def start(self) -> None:
        """Begin a refresh now (at server start), so a cold build is under way early."""
        self._start_or_join()

    def ensure_fresh(self) -> Freshness:
        run = self._start_or_join()
        budget = self._ready_budget if self._ready else self._cold_budget
        finished = run.done.wait(budget)
        elapsed = self._clock() - run.started

        if not finished:
            if self._ready:
                return Freshness(
                    True,
                    _status(
                        f"refresh running {elapsed:.0f}s; edits from the last {elapsed:.0f}s"
                        " may be missing"
                    ),
                    f"refresh running {elapsed:.0f}s",
                )
            return Freshness(
                False,
                _status(
                    f"building, {elapsed:.0f}s so far (about 10s per 200k lines). Retry this"
                    f" call in a few seconds. Repo: {self.root.name}"
                ),
                "building",
            )

        if run.error is not None:
            if self._ready:
                age = self._age()
                return Freshness(
                    True,
                    _status(f"refresh failed: {run.error}; answering from the index of {age}"),
                    f"refresh failed ({run.error})",
                )
            return Freshness(
                False,
                _status(
                    f"build failed: {run.error}. Check that the server's --repo points at a"
                    " checkout; the next call retries."
                ),
                "build failed",
            )
        return Freshness(True, None, "fresh")

    def _start_or_join(self) -> _Run:
        with self._lock:
            if self._running is None:
                run = _Run(self._clock())
                self._running = run
                threading.Thread(target=self._refresh, args=(run,), daemon=True).start()
            return self._running

    def _refresh(self, run: _Run) -> None:
        try:
            self._index_fn(self.root, self.db_path)
        except Exception as exc:  # report to the agent instead of dying silently
            run.error = clip(f"{type(exc).__name__}: {exc}", 120)
        with self._lock:
            if run.error is None:
                self._ready = True
                self._last_ok = self._clock()
            self._running = None
        run.done.set()  # after _running is cleared, so the next call starts a new run

    def _age(self) -> str:
        if self._last_ok is None:
            return "a previous session"
        return f"{self._clock() - self._last_ok:.0f}s ago"


def index_is_ready(db_path: Path) -> bool:
    """True when a complete index exists: the first build has committed.

    During the first build the file already holds the empty schema, so existence
    alone is not enough; the pipeline writes last_indexed_ns in the same
    transaction as the rows.
    """
    try:
        conn = open_index(db_path)
    except IndexNotFoundError:
        return False
    try:
        return get_meta(conn, "last_indexed_ns") is not None
    finally:
        conn.close()


def _status(text: str) -> str:
    return clip(f"[index: {text}]", STATUS_WIDTH)
