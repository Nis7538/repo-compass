# MCP tool reference

`compass serve --repo <path>` exposes six read-only tools over stdio. Every answer is plain
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

Answers that are only an error or a status message (unknown symbol, index still building)
stay under 200 tokens. All six tool definitions together (names, descriptions, input
schemas) stay under 1,200 tokens. A client sends them to the model on every turn.

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

## search_symbols(query, kind=None, limit=10)

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
