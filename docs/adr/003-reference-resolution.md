# ADR-003: Reference Resolution by Name and Imports, at Query Time

## Status
Accepted

## Context
Questions like "who calls `Order.add`?" or "what does this call site point to?" need a
link from a call site to a definition. Doing that exactly requires a compiler front end:
type inference, overload resolution by argument types, inheritance and dispatch. We index
Java and Python with tree-sitter, which gives syntax only. We need something useful,
fast, and honest about how sure it is.

## Decision
1. **Store raw facts, resolve at query time.** The `refs` table stores what the parser saw:
   callee name, receiver text (`order`, `self.repo`, `m.Item`, `super`), argument count,
   location, and the enclosing symbol. `store/resolve.py` computes targets when asked.
2. **Heuristics use only names, imports and scopes**, and every answer carries a tier:

| Tier | Meaning | Examples |
|------|---------|----------|
| exact | Binding is certain from syntax | `self.validate()` inside `Item`; `this.audit(x)`; `Money.round(x)` / `m.Item.parse()` where the receiver names an imported class or module; `persist()` after `from m import store as persist`; `round(x)` after `import static ...Money.round`; `new Order.Line(...)` where `Order` is visible |
| likely | Target's class/module is visible from the calling file, but the receiver is an arbitrary expression, or the target is one inheritance hop up (matched by base-class *name*) | `order.add(item)` where `Order` is imported; `super().save()`; unqualified `log(...)` inherited from `BaseService` |
| possible | Only the name matches; or a Java **static** method called on an arbitrary expression (added in M3) | a method named `total` in a class the calling file never imports; `lookup.get(k)` for static `JavaVersion.get(String)` |

The static-method rule came from the M3 end-to-end run (docs/e2e-m3.md). On commons-lang,
`diff_impact` ranked a change to the static `JavaVersion.get` first, with 66 `likely`
callers outside the diff. All but two were `map.get(...)`-style calls. Java code calls a
static method through its class; calling it through an instance is legal but rare.

3. For each call site only the **best non-empty tier** is kept. So if one candidate is
   exact, weaker candidates are dropped. `find_references(X)` returns the call sites whose
   best tier includes X.
4. **Exclusions** (these remove candidates rather than downgrade them):
   - Java overloads whose parameter count cannot accept the argument count (varargs-aware).
   - Receivers that name a different known class or module (`utils.save()` is never `Item.save`).
   - Java receivers written like a type (`Math`, `java.util.Collections`) that are not
     indexed types. These are JDK or library classes.
   - Python bare-name calls never resolve to methods; attribute calls never resolve to
     module-level functions unless the receiver is that function's module.

### Why query time and not index time
If resolved targets were stored, adding `def save()` to file A could change the correct
target of a call in file B, so B's rows would need rewriting even though B did not change.
That breaks the incremental-index guarantee (edit one file, touch one file's rows) and
makes staleness bugs likely. Resolving at query time is always consistent with the current
index. Its cost is a few indexed lookups per candidate call site, cached per query.

## Known limits (pinned by `test_limit_*` tests in tests/test_resolve.py)
- **No type inference.** `item = It(sku); item.save()` gives `likely` for every visible
  class with a `save` method. Java `lines.add(x)`, where `lines` is a `List` field, is
  still a `likely` match for `Order.add` in files where `Order` is visible. Collection-style
  names (`add`, `get`, `put`) are the main source of false positives.
- **No dispatch or overrides.** A call through an interface or base class resolves to the
  declaration, never to the implementations (`total()` in `Priced` does not reach
  `Order.total`). Overrides are not linked to what they override.
- **Inheritance is one hop, by simple name.** `class A(B)` / `extends B` is matched by the
  text `B`, not by resolving `B` first. Deeper hierarchies degrade to `possible`.
- **Overloads are split by argument count only**, not argument types.
- **Python:** `cls()` / `type(self)()` are not linked to the class. Re-exports through
  `__init__.py` are followed one level, for `from flask import flash` and, since M3, for
  `flask.flash()` (the M3 end-to-end run missed flask's test calls written that way).
  `from x import *` gives `likely`, never `exact`.
  Functions stored in variables, `getattr`, and monkeypatching are invisible.
- **Java:** reflection, dependency injection (Spring), generated code (Lombok getters,
  annotation processors) and members of anonymous classes are invisible. Static-nested
  types inherited from a superclass are not considered visible.
- **Call sites only.** Type usages (parameter types, `extends`, casts, annotations) are not
  references in this model.

## Alternatives Considered
- **Store resolved edges at index time.** Faster queries, but breaks per-file incremental
  updates (see above) or needs a dependency-tracking invalidation scheme that is hard to
  get right.
- **Language servers (jdtls, pyright) or compiler APIs.** Far more precise, but heavy,
  needs a working build or environment for the target repo, and is slow to start. That
  conflicts with "index any checkout in seconds, no external services".
- **Plain name matching (grep-level).** Simple, but common names (`get`, `add`, `save`)
  would drown every answer in noise, and nothing would tell the agent which hits to trust.

## Consequences
- Agents get a confidence tier with every link and can decide how much to trust it. Tools
  (M2) must always show the tier.
- Precision is best when code uses explicit imports and qualified calls. It is worst for
  instance calls on common method names. We will measure it on real repos in the eval
  harness (M5) instead of guessing here. The fixtures are deliberately adversarial and
  their hit rate says nothing about real code.
- The cheapest next improvement is "declared type lookup": in Java, the type of a local,
  parameter or field is usually written next to its name, and that would turn many
  `likely` answers into `exact` or excluded ones. It is deferred until M5 shows whether it
  matters.
