# M2 end-to-end run in Claude Code

M2's acceptance criterion: "works end to end in Claude Code against a real repo; every tool
response under its documented cap". This page records what was run, what happened, and what
it exposed, including the parts that don't flatter the tool. It is a smoke test of the
integration, not an evaluation. Whether compass saves tokens or improves answers is M5's
job, with many tasks and repeated runs.

## Setup (2026-09-29/30)

- Claude Code 2.1.284, headless (`claude -p`, `--output-format stream-json`), model
  `claude-sonnet-5-5`. Windows 11.
- Repositories, cloned into a scratch directory (never modified):
  [apache/commons-lang](https://github.com/apache/commons-lang) @ 29624cd (Java, 628 files,
  207k lines) and [pallets/flask](https://github.com/pallets/flask) @ d73fa1c (Python, 83
  files, 18k lines). These are the same commits as docs/benchmarks.md.
- The server was registered through `--mcp-config` with the command from the README, run
  from inside the target repo:
  `uv run --project C:/Project/Claude/repo-compass compass serve --repo .`. `--repo .`
  resolved to the target repo as intended, and the index went to the user cache directory.
- Runs 1–5 used `--tools ""`, which removes Claude Code's built-in tools, so the six compass
  tools were the only way to look at code. That makes each answer a test of the tools
  alone. Run 6 also allowed Read, Grep and Glob.

Response sizes below are ⌈characters / 3⌉, the same estimate the caps use. They are not
tokenizer counts; the stream only reports usage per turn, not per tool result.

## Runs

| # | Repo | Question (short) | Tool calls (estimated size) | Turns | Cost | Outcome |
|---|---|---|---|---|---|---|
| 1 | commons-lang, **no index yet** | Production callers of `StringUtils.isBlank`, and what it does | 4× "index building, retry" (67 each), get_symbol 215, find_references 641 | 7 | $0.041 | Callers correct. The model miscounted them |
| 2 | commons-lang | Is `lang3` in an import cycle? Which import? Main classes of `builder`? | module_dependencies 227 + 356, search_symbols 1,346, find_references ×2 (largest 872), get_symbol 39 | 8 | $0.052 | Cycle correct. It could not name the import. Class list partly from the model's own memory |
| 3 | flask, no index yet (small) | Request-handling methods of `Flask`; callers of `full_dispatch_request`; is `flask.app` in a cycle? | file_outline 1,333, find_references 107, module_dependencies 198 | 4 | $0.034 | 1 and 2 correct. 3 reported a cycle that only exists for type checkers |
| 4 | flask, after fix | Cycle through `flask.app`? Would it fail at import time? | module_dependencies 181, get_symbol ×2 (26 each) | 5 | $0.025 | Loop correct. The model could not tell whether it runs at import time |
| 5 | flask, after fix | Same as 4 | module_dependencies 188, get_symbol 26 | 3 | $0.012 | Correct and definite: the step is a function-local import, so no import-time failure |
| 6 | flask, **built-in tools allowed** | Callers of `full_dispatch_request`, and what it does | Grep 103, Read ×3 (583, 377, 171) | 5 | $0.063 | Correct. **It did not call any compass tool** |

Every compass response was under its cap. The largest were search_symbols at 1,346
estimated tokens (limit=30, cap 1,500) and file_outline at 1,333 (limit=80, the cap cut it
and it said so). Answers were checked against the source with grep:
- Run 1: `isBlank` has exactly 9 production call sites. find_references listed all 9 as
  `exact`, with no false positives.
- Run 3: `full_dispatch_request` has exactly one caller, `app.py:1600`, as reported.
- Runs 3–5: the import lines named in the cycles were checked one by one.

## What the runs exposed, and what changed

| Finding | Evidence | Change (commit) |
|---|---|---|
| The model subtracted test hits from the total itself and got 13 production callers instead of 9 | Run 1 | find_references header now says `; 7 in 2 test files` (`fix(tools): state test-file share…`) |
| An agent asked which import creates a cycle edge had no way to find out | Run 2 | Each cycle step names one import: `a -> b (A.java:12)` (`feat(tools): name the import behind each step…`) |
| A reported flask cycle went through `cli.py:34`, which is inside `if t.TYPE_CHECKING:` and never runs | Run 3 | The indexer flags TYPE_CHECKING imports (schema v2), and the graph leaves them out: 49 in flask. Its cycle group shrank from 20 modules to 11 |
| The model could not tell whether a cycle step (`cli.py:45`) runs at import time | Run 4 | Steps made only by function-local imports say `inside a function`, and a top-level import is preferred as the example |
| Agents passed import lines to get_symbol to read them | Runs 2, 4, 5 | The reply now says the line is at module level and to read the file |
| flask's `src/flask/sansio/` (no `__init__.py`) was indexed as top-level modules `app`, `scaffold` | Direct tool checks before run 3 | Namespace directories below a regular package are named properly. flask's `route()` call sites went from 285 `possible` to 241 `likely` |
| commons-lang tests share packages with production code and pulled test-only packages into the import graph | Direct tool checks | Test files are left out of module_dependencies. The production cycle is 13 packages |
| One method with 400 calls filled every default find_references row | Stress test, not E2E | The first call site of each caller ranks before repeats |

## Still open (not fixed in M2)

- **With the built-in tools available, Claude chose Grep and Read** (run 6). For a unique
  method name that is a reasonable choice. We did not tune the tool descriptions to steer it
  toward compass; that would bias M5. M5 compares the two conditions on the same tasks.
- **No way to list a package's classes.** In run 2, "main classes of `builder`" came partly
  from the model's own knowledge. `search_symbols` has no path or package filter.
  *Addressed at the start of M3*: `search_symbols(query="", kind="class", path="builder")`
  lists a package's classes. It has not been re-run end to end yet.
- **Inheritance more than one hop away is not resolved** (ADR-003). 44 of flask's
  `@app.route` call sites stay `possible`, because `Flask → App → Scaffold` is two hops and
  those files import only `Flask`.
- **Function-local imports still count as edges** (correctly, since they are real runtime
  dependencies), so flask's cycle group of 11 includes deferred links. The steps say so, but
  the component size does not.
- **Cold start.** The first index of commons-lang after cloning took 33s on this machine
  (7.3s once the OS file cache is warm; see docs/benchmarks.md). Run 1 therefore spent 4
  of its 7 turns on "index building, retry". The retry message worked as designed, but
  each retry costs a turn. Starting the server also takes about 5s here, mostly from
  importing the MCP SDK.
- **The token estimate is not calibrated** against the real tokenizer (ADR-005, decision D2:
  the optional calibration script was not written).
