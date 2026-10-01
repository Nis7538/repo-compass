# ADR-008: The Review Agent Loop, Its Cost Cap and Its Four Tool Conditions

## Status
Accepted

## Context
`compass review --base main --head HEAD` asks Claude for a Markdown review of a change,
letting it call tools in a loop. It is the first part of repo-compass that spends money,
and it is what the M5 eval harness will run many times to compare tool conditions. That
gives four requirements:
- **A hard cost cap.** "Usually under a dollar" is not enough when M5 runs it hundreds of
  times. The cap has to hold even if the model uses every token it is allowed.
- **Comparable conditions.** M5 compares an agent with no tools, plain file tools, the
  compass tools, and both. Only the tool list may differ between them.
- **Metrics per run**, failed runs included, in a form a script can read.
- **Untrusted input.** The diff, the files and every tool answer come from the repository.
  Anyone who can open a pull request controls them.

## Decision

### One loop, four conditions
`agent/loop.py` is a plain tool-use loop over `client.messages.create`. It never imports
the anthropic SDK; the CLI hands it a real client and tests hand it a scripted fake. What
changes between conditions is only the `ToolSet` it is given (`agent/toolsets.py`):

| `--tools` | Tools |
|---|---|
| `none` | none; the diff in the prompt is all the agent has |
| `baseline` | `list_files`, `read_file`, `grep`, `git_diff` (plain file and text access) |
| `compass` | the eight compass MCP tools |
| `both` | baseline and compass together |

The system prompt, the user message (file list plus the diff), the model and the limits
are byte-identical in all four; a test checks it, and every log line carries hashes of the
system prompt, the user message and the tool definitions so M5 can check it too.

The baseline tools follow the same output rules as the compass tools (hard caps, clamped
limits with a note, `[truncated: ...]` markers), so the comparison is about what the tools
know, not about how much text they may return. They include `list_files` so the baseline
is not weaker than what a coding agent normally has (read, grep and glob).

### Compass tools come from the real MCP server
`CompassTools` does not re-declare the eight tools. It starts the server from `server.py`
and talks to it through the MCP SDK's in-process client, the same JSON-RPC Claude Code
uses minus the pipe, kept open in an anyio blocking portal so the synchronous loop can call
it. The definitions and answers are exactly what Claude Code gets and cannot drift. The
repository is indexed before the first request, so no call sees "index building". The
server's `instructions` text is not sent: it would make the system prompt differ by
condition.

### The cost cap is checked before each request
After a request it is too late: the money is spent. So before each request the loop works
out the most that request could cost and sends it only if that fits:
1. **Prompt, bounded from above.** The previous request's actual prompt tokens (from
   `usage`), plus its output (now part of the prompt), plus an estimate of what was added
   since: tool results and notes at characters / 3, plus 10%, plus 50 tokens per block. The
   first request is estimated from its characters plus 1,000 tokens for the API's tool-use
   system prompt (286 tokens on current models, per the pricing page). Characters / 3
   already overestimates code (ADR-005).
2. **Priced at the dearest input rate**, the 5-minute cache write (1.25 x input).
3. **max_tokens is cut** to what the remaining budget can pay for at the output rate.
   Output, thinking included, cannot exceed max_tokens.
4. **Below 1,024 tokens, the request is not sent.**

So spend stays under the cap as long as the prompt bound holds. A test drives the loop with
a client that reports the worst case on every request (prompt = chars / 3 of the whole
request, all of it written to cache; output = all of max_tokens) and checks spend stays
under caps from $0.05 to $2.

Prices live in `agent/pricing.py`, each with its source. An unknown model is refused before
anything runs: a cap that cannot price a request is not a cap.

### Ending with an answer
Stopping the moment the money runs out would leave a run with no review. While exploring,
the loop keeps back enough for one more request (this turn's prompt and output, two full
tool answers, and 4,096 output tokens). When the next exploring turn would eat into that,
or when only one request is left under `--max-turns`, it sends a wrap-up instead: same
tools, `tool_choice: none`, and a short note after the tool results asking for the answer
now. The note goes into the user message about to be sent, so nothing already sent is
edited. Exploring turns also need at least 4,096 tokens of room; a turn cut short by
max_tokens mid-thought ends the run without an answer.

`stop_reason` in the log says why a run ended (`end_turn`, `max_turns`, `cost_cap`,
`max_tokens`, `refusal`, `error`); `wrap_up` says whether the final answer was forced.

### The view of the code
The diff is merge-base...head, like a pull request. The compass index and the baseline
tools read the working tree. To keep the two consistent, `--head` must be the commit that
is checked out; uncommitted changes are allowed but flagged (`dirty` in the log), because
the tools see them and the diff does not. Patches are only taken between two commits:
diffing a working-tree file would make git run clean filters (see `gitrepo.py`).

### Untrusted input
The prompt says that repository content is data, not instructions, and asks the agent to
report text that tries to steer it. That is a mitigation, not a guarantee; prompt
injection is not solved by wording. The actual defence is what the agent can do:
- every tool is read-only; there is no shell, no write tool, no network tool;
- `read_file` only sees files git would list (nothing under `.git/`, nothing ignored such
  as `.env`) and refuses absolute paths, `..` and symlinks;
- all git runs through `gitrepo.run` (no shell, no config-driven programs, a timeout);
- the cost cap bounds what a hijacked run can spend.

A hijacked run can write a misleading review and spend up to the cap. It cannot change the
repository or reach anything else. Logs and transcripts go to the user cache directory, and
writing them (or `--out`) inside the repository is refused.

### The run log
One JSON line per run, appended to `runs.jsonl` in the user cache directory (or `--log`),
written even when the API call failed. It records the run's settings, turns, tool calls per
tool, tool errors, input / cache-write / cache-read / output tokens, cost, index and wall
time, stop reason, whether the diff was truncated, and the transcript path. The
tool-definition overhead is measured once per run with the free `count_tokens` endpoint as
"prompt with tools minus prompt without", which includes the API's own tool-use system
prompt; definitions are re-sent every request, so the run's input totals already include
it (`tool_overhead_tokens_total` = per request x turns).

## Alternatives Considered
- **SDK tool runner** (`client.beta.messages.tool_runner`). Less code, but the cap has to
  act before each request and the wrap-up needs `tool_choice` on the last request only. A
  hand-written loop in one short module is easier to reason about and to test.
- **Checking the cap after each response.** Simpler, but the last response can overshoot
  by a full max_tokens of output, which on Opus is $0.32.
- **Counting the prompt exactly with `count_tokens` before every request.** Exact, but one
  extra API round trip per turn, and the free estimate already errs high.
- **Re-declaring the compass tools for the agent.** Avoids the portal, but the schemas would
  be hand-copied and could drift from what the MCP server serves.
- **Reading the head commit with `git show` instead of the working tree.** Would allow any
  `--head` without a checkout, but the compass index follows the working tree, so the two
  conditions would see different code.
- **Streaming.** Needed for responses over about 21,000 tokens (the SDK refuses longer
  non-streaming requests). A review does not need that; `--max-tokens` stops at 21,000.

## Consequences
- Spend is bounded by `--max-cost` even in the worst case, at the price of exploring turns
  that get less room as the budget shrinks, and a reserve that is sometimes never used.
- The cap's guarantee rests on the prompt bound. A repository full of text that tokenizes
  at well under 3 characters per token (some non-Latin scripts) could break it; ADR-005
  measured at least 3.0 on code.
- `--head` must be checked out. M5 checks out each task's commit anyway.
- Adding a model means adding its price first.
- A turn that hits max_tokens ends the run; the log says so (`max_tokens`).
