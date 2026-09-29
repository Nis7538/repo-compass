# ADR-005: Tool Output Format, Ranking and Token Budgets

## Status
Accepted

## Context
The point of repo-compass is to let an agent answer structural questions with fewer tokens
than grep-and-read. Every token a tool returns is paid for on the call itself, and again on
every later turn while it stays in the context. So output size is a design constraint, not
an afterthought. There are three ways it goes wrong:
- **Too much by default**: bodies or long context in every result, or 100 results when 10 would do.
- **Silent truncation**: the agent cannot tell a cut answer from a complete one.
- **Wasteful encoding**: JSON with repeated keys, long absolute paths, fully qualified names
  on every line.

## Decision

### Progressive disclosure
Listing tools (`search_symbols`, `find_references`, `file_outline`, `module_dependencies`,
`repo_summary`) return locations, signatures and counts only. Source code comes from
`get_symbol`, one symbol at a time, capped at 60 lines by default. The typical agent path is
search → outline or references → one `get_symbol`, not "read the file".

### Plain text, one result per line
There is no JSON. For 10 call sites, pretty-printed JSON with the keys repeated was 2,556
characters. The text format was 1,094 characters, 2.3 times smaller. Models read the text
format as easily. Structure comes from layout:
- results are grouped under their file path, printed once;
- a directory prefix shared by every group is printed once (`paths under src/main/java/com/x/`);
- names are relative to the file's package (`Order.add`, not `com.example.shop.model.Order.add`);
- signatures are clipped at 120 characters, context lines at 80, body lines at 200.

Every tool that takes a path accepts any unique suffix, and every tool that takes a symbol
accepts the short name. So the short forms in an answer can be passed straight back.

### Rank, then cut, and say what was cut
Each tool sorts its results best first and keeps the top `limit` (default 10, at most
200). If the token cap is reached first, it keeps as many as fit. Grouping by file happens
after the cut: groups appear in order of their best entry, so grouping never changes which
results survive. The last line reports the rest, `[truncated: N more (breakdown)] hint`:
the hint says which `limit` would show everything, or that the cap was reached. Headers
always give full totals (for example, call sites per confidence tier), so a truncated
answer still shows the scale.

The rankings (each tool's docs in docs/tools.md say the same):
- `search_symbols`: exact name matches, then full-text word matches (bm25). Production
  code before tests, types before members.
- `find_references`: confidence tier (exact, likely, possible), then production before
  tests, then one call site per calling symbol before any repeats, then location. The
  stress test showed why: without that rule, one method with 400 calls filled the entire
  first page and hid the other 60 calling files.
- `file_outline` and class members in `get_symbol`: types, then callables, then fields.
  The survivors are printed in source order.
- `repo_summary`: packages by lines of code. `module_dependencies`: edges by import count,
  cycles by size.

"Test code" is a path heuristic (`src/test/`, `tests/`, `test_*.py`, `*Test.java`, `*IT.java`).
It only changes the order and never hides anything.

### Hard caps, enforced by a test
Each tool has a cap in `tools/caps.py`: repo_summary 600, search_symbols 1,500, get_symbol
2,000, find_references 2,000, file_outline 1,500, module_dependencies 1,200 tokens. The
renderer counts as it adds lines. It keeps 300 characters free for the truncation marker
and 200 for the index status line (ADR-006), so neither can push an answer over its cap.
The server applies `enforce_cap` to every answer as a last guard. `tests/test_token_caps.py`
calls every tool on a generated repo built to overflow them (a method with 500+ callers, a
300-member class, a 400-line method, a 1,000-character line, a 60-package import cycle), at
the default and the maximum limit, with a worst-case status line. It also fails if
docs/tools.md and `caps.py` disagree.

### Estimating tokens as ⌈characters / 3⌉
There is no offline tokenizer for Claude models, and the caps must be checked in CI
without network access or an API key. Code, paths and camelCase identifiers come to
roughly 3 to 4 characters per token, so dividing by 3 overestimates, which is the safe
direction for a ceiling. This is an estimate, not a measurement: `docs/e2e-m2.md` records
what real responses cost during the Claude Code run. If it turns out to be badly off, the
fix is one constant, `CHARS_PER_TOKEN`.

### No numeric symbol ids
PLAN.md sketched `get_symbol(id | qualified_name)`. Row ids are not stable. Reindexing a
file deletes its rows and inserts new ones (ADR-004), and SQLite can reuse the ids. An agent
holding an id across its own edit could silently get a different symbol. So symbols are
named by what the agent can see: `Order.add`, a qualified name, or `path:line`. If a name is
ambiguous (overloads, or the same method name in several classes), the answer lists the
candidates with `path:line` for each, and costs one more round trip. A stable id (for
example a hash of path, qualified name and overload index) could be added later as a schema
column if the evals show the round trips matter.

### Text only, no structured copy
In mcp 2.x, a tool that returns `str` gets an output schema by default, and the text is sent
twice: as content and as `structuredContent`. Every tool is registered with
`structured_output=False`, and a test checks that `structuredContent` is absent.

## Alternatives Considered
- **JSON output.** Easy for programs to parse, but our consumer is a model, and repeated
  keys cost 2 to 3 times more. It can be added later as an option for the eval harness if
  needed.
- **Full repo-relative paths on every line.** Simpler, but deep Java trees repeat
  `src/main/java/org/...` on every group. Factoring the prefix saved about 30% on the Java
  examples.
- **Soft limits only (a `limit` parameter, no cap).** A single huge signature or a
  1,000-character line would still blow the budget. The cap is what makes a size guarantee possible.
- **Counting tokens with the Anthropic count_tokens API.** Exact, but needs network and a
  key, so it cannot run in CI. It is fine as a one-off calibration.

## Consequences
- Answers are small enough to call tools freely: about 300 to 400 tokens for a typical
  search or reference lookup.
- Agents sometimes need one extra call: `get_symbol` after a search, or a `path:line` after
  an ambiguous name. That is the trade-off progressive disclosure makes on purpose. M5
  measures whether it pays off.
- Caps are only as good as the estimate. They are checked against real responses in the
  E2E write-up, not assumed.
