# M4 end to end: `compass review`

## Status: the real runs have not been done

The M4 acceptance criteria include "a real run on a sample PR completes under the cost cap
and logs metrics". That run has **not** happened. `compass review` calls the Claude API
(the Messages API, through the `anthropic` SDK), and that needs an API key from a Claude
Console account, billed separately from a Claude subscription. The owner has a
subscription only, so no API key was available when M4 was built. That criterion stays
open until someone runs the commands below with a key.

Everything that does not need the API was checked:
- The loop, the cost cap, the wrap-up, the four tool conditions, the run log and the CLI
  are covered by deterministic tests with a scripted client (`tests/test_agent_loop.py`,
  `tests/test_review.py`, `tests/test_agent_toolsets.py`, `tests/test_cli.py`). That
  includes a worst-case client that spends everything each request allows, with spend
  staying under caps from $0.05 to $2.
- The request shape was checked against the installed SDK (`anthropic` 1.9.0):
  `messages.create` accepts `system`, `tools`, `tool_choice`, `max_tokens` and top-level
  `cache_control`, and `messages.count_tokens` accepts `tools`. The SDK refuses
  non-streaming requests above about 21,333 `max_tokens`, so `--max-tokens` stops at
  21,000 (the default is 16,000).
- What the model would be sent was measured offline on the sample PR below (next section).

## The sample PR and the commands

The sample is this repository's own M3 pull request: the merge commit `9c4605f` against its
first parent, so 36 files, +3,433 / -56 lines. Use a separate clone, so the review never
touches a working checkout:

```bash
git clone https://github.com/Nis7538/repo-compass.git /tmp/compass-sample
git -C /tmp/compass-sample checkout -q 9c4605f
uv sync --extra agent
export ANTHROPIC_API_KEY=...          # from the Claude Console; never commit it

for mode in none baseline compass both; do
  uv run compass review --repo /tmp/compass-sample --base HEAD^1 --head HEAD \
    --tools $mode --max-cost 1.00 --out /tmp/review-$mode.md
done
```

Each run prints a summary line to stderr and appends one JSON line to `runs.jsonl` in the
user cache directory (`%LOCALAPPDATA%\repo-compass` on Windows, `~/.cache/repo-compass`
elsewhere), with a transcript next to it under `transcripts/`. Transcripts contain
repository content; keep them out of version control.

**One run per mode is a smoke test, not a comparison.** It shows that each condition runs
to the end, stays under its cap and logs its metrics. It says nothing about which condition
reviews better or cheaper: a single run per condition has no measure of spread, the sample
is one PR, and nobody has judged the reviews. Comparing the conditions is M5's job (many
tasks, N >= 3 runs each, verified answers).

## Measured offline: what each request would carry

Estimated tokens (characters / 3, which overestimates code, ADR-005) for the sample PR:

| Part | Estimate |
|---|---|
| System prompt (same in all modes) | 545 |
| User message: file list + capped diff (same in all modes) | 8,585 |
| Tool definitions, `none` | 0 |
| Tool definitions, `baseline` (4 tools) | 517 |
| Tool definitions, `compass` (8 tools) | 1,455 |
| Tool definitions, `both` (12 tools) | 1,972 |

The API also adds its own tool-use system prompt whenever tools are present (286 tokens on
Opus 5.5, per the pricing page). The run log measures the real overhead with
`count_tokens` instead of using these estimates.

### Finding: on this PR the diff budget is spent on documentation

The user message holds the diff up to 8,000 estimated tokens, whole files in git's path
order, and names the files left out. On this PR, path order puts `CLAUDE.md`, `README.md`
and four `docs/` files first. They use the whole budget, so **30 of the 36 files are left
out, including every source and test file** (`src/compass/tools/impact.py`, +553 lines,
`src/compass/gitrepo.py`, +358, ...). The agent is told which files are missing, so with
tools it can fetch them (`git_diff path=...`, `diff_impact`). In the `none` condition it
can only review the documentation.

This is unfair to `none` on large PRs, and it would bias an M5 comparison. A likely fix is
to fill the budget with source files first, then tests, then everything else. It has not
been made, because it changes the approved "file-list order" rule. It should be decided
before M5.

## Results

None yet. When the runs are done, add a table here straight from `runs.jsonl` (mode, turns,
tool calls, input / cache / output tokens, cost, wall time, stop reason) and a short
honest note on each review, including what it got wrong.
