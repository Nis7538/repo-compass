# MCP tool reference

`compass serve --repo <path>` exposes eight read-only tools over stdio. Every answer is plain
text meant for a model to read. The rules below apply to all of them. The reasons are in
[ADR-005](adr/005-tool-output-format.md).

- **Locations and signatures first.** Only `get_symbol` returns source code.
- **Default `limit` is 10.** Any value up to 200 is accepted. Values out of range are
  clamped, not rejected, and the answer's first line says so:
  `[limit=500 clamped to 200, the maximum]`. Without that line, an agent that asked for 500
  results could take a cut answer for a complete one.
- **Ranked, then cut.** Results are sorted best first. When some are dropped, the last line
  says so: `[truncated: N more (...)] limit=M shows all`. If the token cap cut the list
  before the limit did, the hint says `output cap reached; narrow the query`.
- **Compact.** One result per line. Results are grouped under their file path, and a shared
  directory prefix is printed once as `paths under <prefix>`. Names are relative to the
  file's package (`Order.add`). Signatures are clipped at 120 characters, context lines
  at 80, body lines at 200. There is no JSON.
- **Index status.** A leading `[index: ...]` line appears only when the index is building,
  when an answer may miss very recent edits, or when a refresh failed
  ([ADR-006](adr/006-index-refresh-in-the-server.md)).
- **Naming symbols.** Pass `Class.method`, a fully qualified name, a simple name, or
  `path:line` (the innermost symbol containing that line). Paths can be any unique suffix
  (`model/Order.java`). If a name matches several symbols (overloads, or `save` in four
  classes), the answer lists the candidates to choose from. There are no numeric ids.

## Token caps

Hard ceilings per response, enforced by `tests/test_token_caps.py` on a generated repo
built to overflow every tool. Tokens are estimated as ⌈characters / 3⌉, which overestimates
for typical code (ADR-005). `tests/test_token_caps.py` also checks that this table matches
`src/compass/tools/caps.py`.

| Tool | Hard cap (tokens) | Typical at default limit |
|---|---|---|
| `repo_summary` | 600 | ~250 |
| `search_symbols` | 1,500 | ~300 |
| `get_symbol` | 2,000 | 150–900 |
| `find_references` | 2,000 | ~350 |
| `file_outline` | 1,500 | ~250 |
| `module_dependencies` | 1,200 | ~250 |
| `diff_impact` | 2,000 | 250–800 |
| `hotspots` | 1,200 | ~400 |

Answers that are only an error or a status message (unknown symbol, index still building)
stay under 200 tokens. All eight tool definitions together (names, descriptions, input
schemas) stay under 1,500 tokens (1,322 now). A client sends them to the model on every turn.

## repo_summary(limit=10)

Languages, size, symbol and call-site counts, parse errors, index freshness, then packages
ranked by lines of code. Source roots are printed once. A package whose directory is
`root + package path` gets no directory of its own.

```
repo C:/src/shop  index: fresh
files 16 (java 10, python 6; tests 0)  lines 388  symbols 89  call sites 85  parse errors 1
packages by lines (7 of 7):
source roots (package dir = root + package path): java/shop/src/main/java/, python/src/
  inventory  5 files 158 lines
  com.example.shop.model  5 files 132 lines
  (no package)  python/scripts/  1 file 6 lines
```

## search_symbols(query, kind=None, path=None, limit=10)

`query` is a simple name (`save`), a dotted name (`Order.add`) or words (`get user` finds
`get_user_by_id`). `kind` is one of class, interface, enum, record, annotation, method,
constructor, function, field.

Ranking: exact matches first (simple name equal to the query, or a qualified name equal to
it or ending in `.query`), then full-text word matches in bm25 order. Within the exact
tier, production code comes before test code, types before members, then location. Test
code is guessed from paths (`src/test/`, `tests/`, `test_*.py`, `*Test.java`, ...). It is
only used for ordering and never hides anything.

```
4 symbols match "save" (exact 4)
paths under python/src/inventory/
models.py
  18 method Base.save  def save(self)
  50 method Item.save  def save(self)
services.py
  27 method StockService.save  def save(self)
utils.py
  4 function save  def save(obj)
```

`path` limits the search to part of the repo, matched on whole segments:
- with a `/` or a `.java`/`.py` ending, it is a directory or file anywhere in the tree:
  `lang3/builder`, `model/Order.java`, `inventory/models` (finds `inventory/models.py`).
  A trailing `/` forces this reading (`scripts/`).
- otherwise it is a package or module name, or a dotted piece of one: `builder` finds
  `org.apache.commons.lang3.builder` and its subpackages; `com.x.model`, `inventory.models`.

With `path` and an empty `query`, the tool lists what that part of the repo defines. It uses
the exact-tier ranking (production first, types, then callables, then fields), so
`search_symbols("", kind="class", path="builder")` answers "what are the main classes of
this package" without reading any file. The header counts every kind, so a cut list still
shows the whole:

```
39 symbols under model (5 files): class 3, interface 1, enum 2, record 1, annotation 1, method 12, constructor 5, field 14
paths under java/shop/src/main/java/com/example/shop/model/
Audited.java
  6 annotation Audited  public @interface Audited
Item.java
  3 record Item  public record Item(String sku, double price)
Order.java
  9 class Order  public class Order implements Priced
  62 class Order.Line  public static class Line
[truncated: 35 more (fields 14, methods 12, constructors 5, enums 2, classes 1, interfaces 1)] limit=39 shows all
```

## get_symbol(symbol, max_lines=60)

For a method or function: its dedented source, plus the Javadoc (a Python docstring is
already part of the body). For a class, interface, enum or record: its signature, its doc
and a member outline instead of the body (`max_lines` then limits member lines). A long
body ends with `[truncated: N more lines] read <path> lines A-B`. If the file changed on
disk after it was indexed, a note says the line numbers may be off.

```
method Order.addAll @ java/shop/src/main/java/com/example/shop/model/Order.java:34-39
public Order addAll(Item... items) {
    for (Item item : items) {
        add(item);
    }
    return this;
}
```

## find_references(symbol, limit=10)

Call sites (calls, `new`, method references, `this(...)`/`super(...)`) of one symbol. Each
line shows the calling symbol and the source line. Every call site has a confidence tier
([ADR-003](adr/003-reference-resolution.md)):

- `exact`: certain from syntax (`this.add(x)`, `Money.round(x)` with `Money` imported)
- `likely`: the target's class is visible, but the receiver's type is unknown (`order.add(item)`)
- `possible`: only the name matches

Resolution is name-based with no type inference. Calls on common method names (`add`,
`get`) produce `likely` false positives. They are labeled `likely`, never `exact`.

Ranking: tier (exact, likely, possible), then production before tests, then the first call
site of each calling symbol before that caller's repeats (so a method that calls the target
40 times cannot fill the whole first page), then path and line.
The header always gives the full totals. The truncation marker also names the files that
hold most of the cut call sites. Example with 40 callers at the default limit, about 330
tokens:

```
method Order.add @ src/main/java/com/example/shop/model/Order.java:25  public Order add(Item item)
40 call sites in 17 files (exact 4, likely 33, possible 3); 5 in 1 test file
rank: exact > likely > possible, non-test first, new callers before repeat calls
paths under src/main/java/com/example/shop/
model/Order.java
  61 exact Order.addAll: for (Item i : items) add(i);
  88 exact Order.merge: other.items().forEach(this::add);
  97 exact Order.addIfAbsent: if (!contains(item)) add(item);
  103 exact Order.repeatLast: add(last);
service/CartService.java
  44 likely CartService.addToCart: order.add(item);
  71 likely CartService.restore: restored.add(catalog.find(sku));
service/CheckoutService.java
  52 likely CheckoutService.applyBundle: order.add(bonusItem);
service/ReorderService.java
  33 likely ReorderService.reorder: next.add(previous.itemAt(i));
web/OrderController.java
  58 likely OrderController.addItem: orders.get(id).add(toItem(request));
  90 likely OrderController.bulkAdd: order.add(item);
[truncated: 30 more (likely 27, possible 3; 13 files, most in ImportJob.java 6, OrderServiceTest.java 5, InventorySync.java 3)] limit=40 shows all
```

## file_outline(path, limit=10)

What a file defines, with line ranges and signatures, as a tree. When truncated, types are
kept first, then methods, functions and constructors, then fields. The survivors are
printed in source order. The kind word is omitted when the signature already spells it out
(`class Line`).

```
java/shop/src/main/java/com/example/shop/model/Order.java (java, 83 lines, module com.example.shop.model)
  9-83 public class Order implements Priced
    16-18 constructor public Order()
    25-27 method public Order add(Item item)
    62-74 public static class Line
[truncated: 13 more (fields 8, methods 4, constructors 1)] limit=23 shows all
```

## module_dependencies(module=None, limit=10, include_tests=False)

The import graph between modules: Java packages and Python modules. Edge weight is the
number of import statements. External modules are collapsed (`java.util`, `requests`).
Cycles are strongly connected components, largest first, each shown as one concrete loop.
After each step, the loop names one import statement that creates that edge
(`a -> b (A.java:12) -> a (B.java:40)`), since the next question is usually which import
to remove. A top-level import is preferred as the example. If every import making a step
is inside a Python function, the step says `(cli.py:45, inside a function)`, because such
an import runs only when the function is called, and the cycle cannot fail at import time. It says `cycle among 13 modules, e.g. ...` when the loop doesn't pass through
every member.

Python imports inside `if TYPE_CHECKING:` are left out, because they never run. Counting
them reports cycles that exist only for type checkers; flask's `flask.app <-> flask.cli`
went through one. Test files are left out of the graph too by default, and the header says
how many of each were left out. Java tests usually live in the same packages as the code
they test, so their imports would show up as production dependencies. On
apache/commons-lang they joined test-only packages into the main cycle.
`include_tests=True` puts test files back, for questions like "which tests import this
module?". The header then says `N test files included`.

- No `module`: counts, the most imported modules, cycles, and the heaviest edges.
- `module`: its internal imports, who imports it, its external dependencies, and the cycles
  it is part of. Pass `pkg.*` (or a prefix that is not itself a module) to treat a whole
  package as one node. Edges inside the group are then hidden.

```
module inventory.models (1 file)
imports 1 internal: inventory.helpers 1
imported by 3: inventory.services 4, seed 2, inventory 1
external 3: dataclasses 1, fastjson 1, typing 1
cycles: none
```

Limits: Java classes used from the same package, or written fully qualified without an
import, create no edge. Python imports inside functions count like top-level imports.
Dynamic imports are invisible.

## diff_impact(base, head=None, limit=10)

What a change touches, and who outside it depends on what changed. Like a pull request,
the diff runs from the merge base of `base` and `head` (`base...head`) to `head`. `head`
is a branch, tag or commit; the default is the working tree, uncommitted and untracked
files included. So `diff_impact("main")` answers "what can my current edits break?".

1. **Files** are compared by git blob id. There is no rename detection: a moved file is one
   deletion and one addition.
2. **Symbols.** Both versions of each changed Java/Python file are parsed with the
   indexer's own extractors and compared symbol by symbol. This part is exact. A symbol is
   `removed`, `added`, `signature` (declaration text, decorators/annotations, parameter
   count) or `body` (its own lines, without its children's and without any whitespace).
   So a changed method does not also mark its class. A method that was only moved,
   re-indented or reformatted is not reported. Symbols are matched by qualified name across
   all changed files, so a class moved to another file of its package is not a change.
   Editing a member's Javadoc does not mark the class; a comment edit inside a method body
   does count.
3. **Callers** come from the resolver, with the same tiers as `find_references`, so this
   part is approximate. Each caller is *inside* the diff (its file changed too, so it was
   probably updated) or *outside* (untouched: the real risk). For a **removed** symbol, the
   old version is put back into the resolver, so the call sites that would still bind to
   it are found: the uses the change left **dangling**. Imports that name it are counted
   too. For a **signature** change, callers of the old and the new declaration are both
   counted. A Java call that still passes the old number of arguments no longer binds to
   the new method, and it is the call most likely to be broken.
4. **Ranking:** removed and signature changes with exact or likely callers outside the diff
   (or any dangling use), then body changes with such callers, then the other removed and
   signature changes, then the other body changes, then added symbols. Within a group:
   production before tests, then more exact+likely callers. `possible` callers are counted
   and shown but never raise a symbol's rank: a method named `process` with 100 name-only
   matches must not top the list.

Each entry is at its line in `head` (`-14` = removed, the line it had in the base), with up
to three example callers after `<-`, best first. The full list is one `find_references`
away. The last line lists files outside the diff that import a touched module. Example from
the test history (tests/shop_history.py), 450 tokens:

```
diff main...HEAD (merge base 6e9cdb5): 4 code files changed (1 test), 1 other file; 7 symbols changed (removed 2, signature 1, body 3, added 1)
modules touched 3: com.example.shop.model, com.example.shop.service, inventory.stock
inventory/stock.py
  -11 removed function release  dangling: 1 call (exact 1), 1 import
    <- sync (sync.py:9)
    imported at: sync.py:3
shop/src/main/java/com/example/shop/model/Order.java
  -14 removed method Order.addIfAbsent  dangling: 2 calls (likely 2), 0 imports
    <- ImportJob.run (ImportJob.java:10), OrderTest.addsOnce (OrderTest.java:6)
  9 signature method Order.add  public Order add(Item item, int qty)
    was: public Order add(Item item)
    callers 3, outside the diff 1 (likely 1)
    <- ImportJob.run (ImportJob.java:9)
  16 body method Order.total  callers 3, outside the diff 1 (likely 1)
    <- SalesReport.sum (SalesReport.java:7)
  24 added method Order.clear  public void clear()
shop/src/main/java/com/example/shop/service/CartService.java
  9 body method CartService.addToCart  callers 0
shop/src/test/java/com/example/shop/model/OrderTest.java
  9 body method OrderTest.totals  callers 0
importers of touched modules outside the diff: 3 files (0 tests): inventory/sync.py, shop/src/main/java/com/example/shop/job/ImportJob.java, shop/src/main/java/com/example/shop/report/SalesReport.java
```

`Order.add` has 3 callers although only CartService and ImportJob call it: `items.add(item)`
inside `Order.add` itself is counted as a likely call too. That is the known false positive
for common method names ([ADR-003](adr/003-reference-resolution.md)); it is inside the
diff, so it does not affect the ranking.

The header totals never shrink with the limit. The truncation marker says what kinds were
cut and whether any of them had callers outside the diff:
`[truncated: 4 more symbols (body 3, added 1; 1 with callers outside the diff)] limit=7 shows all`.
When a long signature changes past the 120-character clip, both versions are clipped from
just before the first difference (`…argumentNumber5, int extra)`).

Answers without a diff are one line: `Unknown ref "mian". Branches: main, feature, tweak.
Also works: a tag, a commit, HEAD~3.`, `No changes between main and main.`, `No Java or
Python files changed (1 other file).`, `other and working tree share no history (no merge
base).`, `Not a git repository: ... the other tools work without it.` A ref starting with
`-` is refused, since git would read it as an option.

Limits:
- Callers and importers come from the index, which follows the working tree. For a `head`
  other than the checked-out commit, the header says so.
- No override or dispatch analysis: changing an interface method does not flag its
  implementations, and callers through the interface are found only by name.
- Field uses are not indexed (`uses not indexed`). Java classes used from their own package
  need no import, so the importers line undercounts them.
- Module-level code that is not a symbol (imports, constants, statements) is not reported;
  the file still counts as changed.
- An edit that only adds or removes spaces inside a string literal is missed (whitespace is
  ignored).
- Working-tree files are compared with their committed blob as they are, and with CRLF
  turned into LF (a `core.autocrlf=true` checkout). Other checkout conversions (`ident`,
  `working-tree-encoding`, smudge filters) are not undone, so such files show as changed.
- At most 300 changed code files are parsed (by path), and callers are looked up for at most
  200 changed symbols (removed and signature changes first). The header says when either
  bound was hit.

## hotspots(since=None, limit=10, include_tests=False)

Files that change often and hold a lot of code, which is where bugs tend to collect.

- **score = churn × size**, per file.
- **churn**: non-merge commits in the window that touched the file. Renames are followed
  forward, so commits made under an old name count for today's path. Only indexed files
  count; deleted files drop out.
- **size**: lines inside outermost methods, functions and constructors, from the index. A
  nested function is not counted twice. Imports, constants and data declarations are left
  out, so a 900-line constants file does not outrank real logic.
- Ties go to more commits, then path. Each line names the file's longest method, which is
  where to point `get_symbol` next, and the date of its latest commit in the window.
- **since**: a ref (`v1.2`, `main~50`: the commits after it, up to HEAD) if it names a
  commit, otherwise a date git understands (`6 months ago`, `2026-01-01`; a bare date means
  midnight UTC). Default: `1 year ago`. The header shows the date git read, because git's
  date parser never fails: `yesterdya` is read as today, and a year after 2099 as January
  1st of this year.

Example from the test history, 316 tokens:

```
hotspots since 2026-01-01: 7 commits (merges skipped), 9 of 9 indexed files changed; left out: 1 test file
score = commits x lines inside methods/functions, per file. Renames followed.
  114 = 6 x 19  shop/src/main/java/com/example/shop/model/Order.java  largest Order.total 7 lines, last 2026-03-02
  18 = 3 x 6  shop/src/main/java/com/example/shop/service/CartService.java  largest CartService.addToCart 3 lines, last 2026-03-02
  8 = 2 x 4  inventory/sync.py  largest sync 4 lines, last 2026-01-19
  6 = 2 x 3  inventory/stock.py  largest reserve 3 lines, last 2026-03-02
  6 = 1 x 6  shop/src/main/java/com/example/shop/job/ImportJob.java  largest ImportJob.run 6 lines, last 2026-01-05
  3 = 1 x 3  shop/src/main/java/com/example/shop/report/SalesReport.java  largest SalesReport.sum 3 lines, last 2026-01-05
  0 = 1 x 0  inventory/__init__.py  last 2026-01-05
  0 = 1 x 0  shop/src/main/java/com/example/shop/model/Item.java  last 2026-01-05
```

`inventory/sync.py` has 2 commits: one made when it was still `sync_job.py`, and its rename.

With no commits in the window: `No commits since "2027-01-01" (read as 2027-01-01). since
takes a date git understands ("6 months ago", "2026-01-01") or a ref ("v1.2").`

Limits: per file, not per method. It counts commits, not lines changed, so a mass reformat
counts like any other commit. Uncommitted edits are not churn. Sizes come from the working
tree. A shallow clone undercounts, and the header says `note: shallow clone`. At most 5,000
commits are read, and the header says when that bound is hit. Test files are left out
unless `include_tests=True`, and the header says how many.

## Git safety

The git tools only read. `src/compass/gitrepo.py` is the only place that starts git. It
runs without a shell, with a timeout, with `GIT_OPTIONAL_LOCKS=0` so `.git/index` is not
refreshed as a side effect, with `--no-pager`, and with `-c core.fsmonitor=false -c
log.showSignature=false`. `git log`, `git diff` and `git grep` also get `--no-ext-diff`
and/or `--no-textconv`. Git is never asked to diff a working-tree file, because a clean
filter named in `.gitattributes` would run, and no flag turns that off. Working-tree files
are hashed in Python instead. (`git grep` reads working-tree files as they are, without
filters.) `tests/test_git_safety.py` sets every one of these hooks to a command that leaves
a marker file, checks that indexing, both tools and the review agent's git calls leave
none, and checks that plain git does fire them.

## Baseline tools (review agent only)

`compass review --tools baseline` (and `both`) gives the agent four plain tools instead of,
or next to, the compass tools: roughly what a coding agent has without compass. They are
not served over MCP. M5 compares the conditions, so these tools follow the same rules as
the compass tools: plain text, a hard cap per response, out-of-range limits clamped with a
note, and a `[truncated: N more ...]` line when something is cut. They know nothing about
symbols, call sites or imports. Code: `src/compass/tools/baseline.py`.

| Tool | Hard cap (tokens) | Arguments |
|---|---|---|
| `list_files` | 1,500 | `glob=None, limit=100` (max 500) |
| `read_file` | 2,000 | `path, start_line=1, max_lines=200` (max 400) |
| `grep` | 2,000 | `pattern, path=None, fixed=False, limit=50` (max 200) |
| `git_diff` | 2,000 | `path=None, context=3` (max 10) |

- **What they can see.** Only files git would list: tracked, plus untracked and not
  ignored. Nothing under `.git/`, nothing ignored (a `.env` file, build output).
  `read_file` also refuses absolute paths, `..`, symlinks and binary files.
- `list_files` groups paths by directory. `glob` is matched against the whole path, where
  `*` also crosses `/` (`*.py`, `src/*`); a glob ending in `/` is a directory.
- `read_file` prints numbered lines; lines longer than 300 characters are cut, not
  collapsed, so indentation survives. The marker says where to continue:
  `[truncated: 120 more lines] start_line=201 continues`.
- `grep` is `git grep -E` (or `-F` with `fixed`) over the working tree, matches grouped by
  file in path order, lines clipped at 200 characters. No ranking.
- `git_diff` is the change under review (merge base to head), whole or for one path. When
  the cap cuts it, the marker names the files not shown.
