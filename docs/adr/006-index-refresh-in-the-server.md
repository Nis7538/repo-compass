# ADR-006: Refreshing the Index Inside the MCP Server

## Status
Accepted

## Context
The MCP server runs for a whole agent session while the agent edits code. If it answered
from an index built at startup, `get_symbol` would show old bodies and `find_references`
would miss new callers. That is worse than no tool, because the agent trusts the answer.
There are two constraints:
- A tool call must not hang. Clients time out, and a slow tool makes agents give up on it.
- On a fresh machine there is no index at all, and the first build of a large repo takes
  seconds (7.3s for 207k lines, docs/benchmarks.md).

## Decision
`compass/index_manager.py` runs the M1 incremental indexer (`index_repo`) in a background
thread and makes each tool call wait for it only for a bounded time.

1. **At server start** a refresh begins right away, so a cold build is already running when
   the first call arrives.
2. **On every tool call** the server starts a refresh, or joins the one already running
   (single flight: one writer, concurrent calls share it). Then it waits:
   - **An index exists:** up to 1.5s. If the refresh finishes, the answer is fresh and gets
     no extra text. If not, the tool answers from the last committed index and prepends
     `[index: refresh running 4s; edits from the last 4s may be missing]`.
   - **No index yet:** up to 5s. If the build finishes, the call answers normally. If not,
     the only reply is `[index: building, 6s so far (...). Retry this call in a few seconds.]`.
3. **Failures** are reported, not swallowed. With an old index, the answer carries
   `[index: refresh failed: <error>; answering from the index of <age>]`. Without one, the
   reply says the build failed and to check `--repo`. The next call retries.
4. **"Ready" means the first build committed.** The indexer creates the schema before the
   big transaction, so during a first build the database exists but is empty. Ready is
   `meta.last_indexed_ns` being present, which is written in the same transaction as the rows.

Why readers are safe while the refresh writes: the database is in WAL mode, so readers are
never blocked by the writer. The whole refresh is one transaction (ADR-004), so a reader
sees either the old index or the new one, never a mix. Each tool call opens its own
connection. The SDK runs sync tools in worker threads, and SQLite connections are not
shared between threads.

### No throttle
Every call refreshes. A no-change refresh only runs `git ls-files` and stats files: 0.07s
at 50k lines, 0.12s at 207k lines. A throttle ("at most once every N seconds") would save
that time but would miss an edit the agent made a moment ago, which is exactly the case
freshness exists for. If very large repos make refreshes slow, the fix is to add a minimum
interval in `IndexManager._start_or_join`. The status line already tells the agent when
an answer may be stale.

## Alternatives Considered
- **Refresh synchronously on every call.** Always fresh, but a cold build or a big branch
  switch blocks the call for many seconds.
- **Index only at startup and require `compass index` by hand.** Simple, but it silently
  serves stale answers after the agent's own edits.
- **File watcher (watchdog / inotify).** Refreshes proactively, but it is a new dependency,
  has platform-specific behavior, and still needs the same "is it done yet" logic for the
  first build. ADR-004 already rejected watching for the CLI.
- **Refuse to answer while refreshing.** Honest, but most refreshes change nothing the
  question is about. A stale answer with a clear note is more useful than no answer.

## Consequences
- A typical call adds about 0.1s of refresh time before the query runs.
- An agent can get an answer that misses edits from the last few seconds. It is always told
  when this can happen.
- Tests drive `IndexManager` with a fake indexer that blocks on an event, so "slow refresh"
  and "failed refresh" are deterministic (tests/test_index_manager.py).
