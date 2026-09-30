# ADR-007: diff_impact and hotspots

## Status
Accepted

## Context
M3 adds two tools that need git history, which the index does not have.
`diff_impact(base, head)` answers "what does this change touch, and who outside it depends
on what changed?". `hotspots(since)` answers "where does code churn?". Both follow the
ADR-005 rules: rank, then cut, say what was cut, hard cap enforced by a test.

Running git inside a repository an agent points us at raises three problems:
- **Git can run programs from the repo's config.** `.git/config` can name an fsmonitor
  hook, an external diff, gpg for signatures and a pager. `.gitattributes`, which is
  committed and arrives with every clone, can name textconv drivers and clean filters.
  Plain `git diff` against the working tree runs a clean filter, and no command-line flag
  turns that off.
- **Read-only must stay true.** `git status` and `git diff` refresh `.git/index` as a side
  effect.
- **Line diffs are the wrong unit.** An agent wants "Order.add changed its signature", not
  hunk headers.

## Decision

### 1. Compare blob ids, never ask git to read the working tree
`src/compass/gitrepo.py` is the only place that starts git. It never runs `git diff`.
`git ls-tree -r` gives the blob id of every file at the base and, for a commit, at the
head. For the working tree, each file listed by `git ls-files` is read and hashed in Python
(`sha1("blob <size>\0" + bytes)`, what git would store). Files whose ids differ changed.
A checkout with `core.autocrlf=true` writes CRLF files whose blobs have LF, so each file's
id is also computed with CRLF turned into LF. A test compares both ids with
`git hash-object` for LF and CRLF files, with and without a trailing newline.

Every git call also passes `-c core.fsmonitor=false -c log.showSignature=false`,
`--no-pager`, `GIT_OPTIONAL_LOCKS=0`, a timeout and no shell. `git log` also gets
`--no-ext-diff --no-textconv`. Refs starting with `-` are refused. `tests/test_git_safety.py`
sets every hook to a command that leaves a marker file, runs indexing and both tools, and
checks that no marker exists. It then runs plain git and checks that the markers do appear,
so the test cannot pass because the hooks were broken.

### 2. Changed symbols are exact: parse both versions
Both versions of each changed file are parsed with the indexer's own extractors, and
symbols are matched by (qualified name, kind) across all changed files. Overloads are
paired by identical declaration first, then in order. A symbol is removed, added, a
signature change (declaration text, annotations/decorators, parameter count) or a body
change. "Body" compares the symbol's own lines, meaning its lines minus its children's,
with all whitespace removed. So a changed method does not mark its class, and a moved,
re-indented or reformatted method is not reported.

### 3. Callers are approximate, and say so
Callers come from the ADR-003 resolver with its tiers, split into inside the diff (the
caller's file changed too) and outside (untouched). For a removed symbol, the resolver has
nothing to find, so the old version is added back as a virtual row with a negative id
(`Resolver.add_virtual`). The call sites that would bind to it are the uses the change left
dangling, found by the same rules as every other reference. For a signature change,
callers of both the old and the new declaration are counted. That matters in Java: a call
with the old number of arguments no longer binds to the new method, and it is the call most
likely to be broken.

### 4. Rank by evidence, and never let name-only matches promote
Removed/signature changes with exact or likely callers outside the diff come first, then
body changes with such callers, then the rest, and added symbols last. `possible` callers
are shown but never raise a symbol's group. Otherwise a method named `process` with 100
name-only matches would top every diff.

### 5. Merge-base diff, working tree by default
Like a pull request, the diff runs from the merge base of base and head, so commits that
landed on `main` since the branch point are not blamed on the branch. `head` defaults to
the working tree, which covers "what can my current edit break?".

### 6. hotspots = commits x lines inside methods
Churn is non-merge commits in the window that touched the file, with renames followed
forward to today's path. Size is lines inside outermost methods, functions and
constructors. It is not file length, so a 900-line constants file does not outrank real
logic. `since` is a ref if it names a commit, else a git date; the header shows the date
git read, because git's date parser never fails.

## Alternatives Considered
- **`git diff --name-status` for the file list.** Simpler, but against the working tree it
  runs clean filters, so a hostile `.gitattributes` could run code. The blob comparison
  costs one Python hash per tracked file (measured in docs/e2e-m3.md).
- **Symbols from diff hunks.** Map changed line ranges onto index symbols. Cheaper, but it
  cannot tell a signature change from a body change, sees removed symbols only through
  line numbers of a version the index no longer has, and reports every moved method.
- **Rename detection (`-M`) in diff_impact.** Matching symbols by qualified name across all
  changed files already covers a class moved within its package. A moved Python file
  changes its module name, and its importers really do break, so treating that move as
  removed + added is the honest answer.
- **Weighting churn by lines changed.** It is more precise for mass reformats, but it needs
  `--numstat` and a patch for every commit, and reformats should be rare. The limitation
  is documented instead.
- **A separate resolver for the base version.** It would need a second index of the base
  commit. Re-adding the removed symbols as virtual rows reuses everything with a
  twenty-line change.

## Consequences
- An agent gets "removed `Order.addIfAbsent`, 2 dangling calls, here they are" in about
  450 tokens, instead of reading a diff and grepping for each name.
- Callers are from the working-tree index even when `head` is another commit; the header
  says so. No override or dispatch analysis: changing an interface method does not flag
  its implementations.
- Each diff_impact call starts about ten short git processes and hashes the tracked files.
  That is under a second on the test repos. On Windows each git process costs about 50ms.
- Hotspots counts commits, not lines, and undercounts in shallow clones (flagged).
- The tool-list budget grew from 1,200 to 1,500 tokens (ADR-005): the eight tool
  definitions measure 1,322.
