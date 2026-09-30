# M3 end-to-end runs: diff_impact and hotspots

What was run with the two git tools on real repositories, what came out, and what it
exposed. Like docs/e2e-m2.md, this is a smoke test of the integration, not an evaluation:
one run per question, answers spot-checked with grep. Whether the tools save tokens or
improve answers is M5's job.

## Setup (2026-09-30 / 10-01)

- Claude Code 2.1.285, headless, through `scripts/e2e_claude.py` (manual only, never in
  CI). That script runs `claude -p` with `--output-format stream-json`, model
  `claude-sonnet-5-5` (the same as M2), and the compass server registered through
  `--mcp-config`, started from inside the target repository with `--repo .`. Built-in
  tools were removed (`--tools ""`) except in run 6. Windows 11.
- Repositories cloned into a scratch directory, the same commits as docs/benchmarks.md:
  [apache/commons-lang](https://github.com/apache/commons-lang) @ 29624cd (Java, 628
  files) and [pallets/flask](https://github.com/pallets/flask) @ d73fa1c (Python, 83
  files). The clones were never modified. Runs 4–6 used a copy of flask with two
  uncommitted edits in `src/flask/helpers.py`: `get_flashed_messages` deleted, and the
  `category="message"` default of `flash()` removed.
- Sizes are ⌈characters / 3⌉, the estimate the caps use.

## Runs

| # | Repo | Question (short) | Tool calls (estimated size) | Turns | Cost | Outcome |
|---|---|---|---|---|---|---|
| 1 | flask | Which changes in HEAD~20..HEAD could affect code outside them? Riskiest one and its callers? | diff_impact 733 | 2 | $0.034 | Correct. `Flask.test_request_context`, 5 callers outside the diff. grep finds exactly those 5 call sites |
| 2 | commons-lang | Same range. What could break production code? | diff_impact 1,119 (limit=20) | 3 | $0.032 | **Misled.** Called `Map.get` sites the "widest blast radius" of the static `JavaVersion.get`. Fixed, see below |
| 3 | commons-lang | Riskiest production hotspots over the last year; longest method in the top one, and what it does? | hotspots 447, get_symbol 140 (overloads listed) + 1,798 | 4 | $0.036 | Correct. StringUtils 59 × 2,881; `getLevenshteinDistance`, 134 lines, explained from its source |
| 4 | flask + edits | What could my uncommitted changes break? | diff_impact 250 | 2 | $0.021 | **Partly wrong.** Both breaks and both imports were found, but it said the removed function had no call sites left (grep: 6 in tests), and missed 6 of 10 `flash` callers. Fixed, see below |
| 5 | flask + edits | Same as 4, after the fixes | diff_impact 292 | 2 | $0.012 | Correct. 6 dangling calls + 2 imports, 10 exact `flash` callers (6 in tests). All match grep |
| 6 | flask + edits | Same, **with Read/Grep/Glob allowed** | Bash (refused) 45, diff_impact 306, find_references 28 + 297, Grep 304 | 6 | $0.055 | The most complete answer. It called diff_impact first, then used Grep to find the Jinja template use and the `app.py:498` reference that the index cannot see. It miscounted the one-argument `flash` calls ("five", then listed seven) |

In M2's run 6, the model ignored compass once Grep and Read were available. Here it started
with diff_impact. One run proves nothing about preference; M5 measures that.

## Direct tool runs (no model)

Measured in-process on the same commits, after the fixes below. Times are one warm run on
this machine; git processes cost about 50ms each on Windows.

| Repo | Call | Time | Size |
|---|---|---|---|
| commons-lang | `diff_impact("HEAD~20", "HEAD")`: 74 code files, 42 symbols | 3.9s | 762 |
| commons-lang | `diff_impact("HEAD~20")` (working tree) | 3.4s | 764 |
| commons-lang | `hotspots()` (1 year, 1,116 commits) | 0.6s | 447 |
| commons-lang | `hotspots("5 years ago")` (3,488 commits) | 0.9s | 449 |
| flask | `diff_impact("HEAD~20", "HEAD")`: 17 code files, 43 symbols | 1.2s | 765 |
| flask | `diff_impact("HEAD~1", "HEAD")` | 0.7s | 221 |
| flask | `hotspots()` | 0.35s | 386 |

Where diff_impact's time goes on commons-lang (profiled): parsing both versions of 74
files takes about two thirds. That includes `StringUtils.java` and `ArrayUtils.java`,
about 9,000 lines each. Caller lookups (18 symbols, 1,627 call sites resolved) and git
take about 1s each. Hashing the working tree instead of comparing two commits costs
0.9–1.7s for commons-lang's 718 tracked files, against 0.2s commit to commit.
No response was above its cap. The largest was 1,119 of 2,000, at limit=20.

## What the runs exposed, and what changed

| Finding | Evidence | Change (commit) |
|---|---|---|
| A Python signature included a comment after the colon, so a comment edit was reported as a signature change | Direct run, flask `test_load_dotenv` | Signatures stop at the colon; schema v3 rebuilds old indexes (`fix(indexer): Python signatures stop at the colon`) |
| A decorator-only change printed two identical lines | Direct run, flask `test_method_route` (a `parametrize` change) | Both lines lead with the decorators when only they changed (`fix(tools): show decorators…`) |
| A long signature changed at its end showed two identical clipped lines | Token-cap stress repo | Both are clipped from just before the first difference (`fix(tools): show where a long changed signature differs`) |
| The static `JavaVersion.get` had 66 `likely` callers outside the diff, nearly all `map.get(...)` calls, and ranked first | Direct run, commons-lang | A Java static method called on an arbitrary expression is `possible` (ADR-003). It now has 178 `possible` callers and no longer ranks first (`fix(resolve): a Java static method called on an expression is possible`) |
| The model reported name-only matches as real callers, because diff_impact showed them as examples | Run 2 | Examples are exact or likely callers only. An all-`possible` count says `name matches only` (`fix(tools): diff_impact examples are exact or likely callers only`) |
| `flask.flash(...)` and `flask.get_flashed_messages()` (a call through the package's re-export) did not resolve, so 6 dangling test calls and 6 `flash` callers were missed | Run 4, checked with grep | The re-export is followed for `module.name()` calls too (`fix(resolve): follow a package re-export for module.name() calls`) |
| The imports of a removed function shared the three example slots with calls, so they were never named. Here, those imports make `import flask` fail | Run 5 | An `imported at:` line of its own (`fix(tools): name the imports of a removed symbol…`) |

## Still open

- **Only Python and Java are indexed.** A removed function that a Jinja template calls
  (`base.html`), or that is referenced without a call (`get_flashed_messages=...` in
  `app.py:498`), does not show up. In run 6 the model found both with Grep; in run 5 it
  said it had not checked templates.
- **Common method names stay noisy.** An instance method named `get` or `add` still collects
  `likely` false positives ([ADR-003](adr/003-reference-resolution.md)). The static rule
  only helps static methods.
- **diff_impact takes seconds on large diffs**, mostly from parsing both versions of big
  files. That is fine for a PR review. It could matter if an agent calls it after every
  edit. A per-blob parse cache would help, and M5 will show whether it is needed.
- **Hotspots counts commits, not lines**, and counts a file, not a method. On
  commons-lang, `StringUtils.java` tops every window because it is big, and a
  method-level churn score might rank differently. This is documented, not fixed.
- **One run per question.** These are anecdotes, not measurements. M5 runs each task
  several times, with and without compass.
