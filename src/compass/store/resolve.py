"""Name-based reference resolution with import/package heuristics.

The index stores call sites as raw facts (callee name, receiver text, location).
This module decides, at query time, which symbol(s) a call site may refer to and
how sure we are:

  exact     the binding is syntactically certain (self/this call into the enclosing
            class, a receiver that names an imported class or module, a name
            imported from the target's module, a constructor of a visible class)
  likely    the target's container is visible from the calling file, but the
            receiver is an arbitrary expression whose type we do not know; or the
            target is found one inheritance hop up, by simple base-class name
  possible  only the name matches

Only the best non-empty tier is returned for a call site. There is no type
inference, no override/dispatch analysis and no data flow; see
docs/adr/003-reference-resolution.md for the full list of limits.
"""

import re
import sqlite3
from dataclasses import dataclass

from compass.indexer.models import TYPE_KINDS
from compass.store.queries import (
    REF_SELECT,
    SYMBOL_SELECT,
    ImportRow,
    RefRow,
    SymbolRow,
    ref_row,
    symbol_row,
)

EXACT = "exact"
LIKELY = "likely"
POSSIBLE = "possible"
RANK = {EXACT: 3, LIKELY: 2, POSSIBLE: 1}

_PY_CALLABLE = frozenset({"function", "method", "class"})
# Java naming convention for a type reference: optional lowercase package, UpperCamel name.
_EXTERNAL_TYPE = re.compile(r"([a-z_][\w]*\.)*[A-Z]\w*")


@dataclass(frozen=True)
class Resolution:
    symbol: SymbolRow
    confidence: str


@dataclass(frozen=True)
class Reference:
    ref: RefRow
    confidence: str


def simple_name(type_text: str) -> str:
    """'Base<T>' -> 'Base', 'abc.ABC' -> 'ABC', 'Generic[T]' -> 'Generic'."""
    return re.split(r"[\[<(]", type_text)[0].rsplit(".", 1)[-1].strip()


class Resolver:
    """Resolves call sites against one index. Caches lookups; create one per query batch."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._symbols: dict[int, SymbolRow] = {}
        self._by_name: dict[tuple[str, str], list[SymbolRow]] = {}
        self._by_qn: dict[str, list[SymbolRow]] = {}
        self._imports: dict[int, list[ImportRow]] = {}
        self._py_bindings: dict[int, dict[str, str]] = {}
        self._py_modules: dict[str, bool] = {}
        self._reexports: dict[str, str] = {}
        self._java_types: dict[tuple, str | None] = {}

    # --- public API ------------------------------------------------------------------

    def resolve_ref(self, ref: RefRow) -> list[Resolution]:
        """Symbols this call site may refer to, all in the best tier found."""
        enclosing = self._enclosing_classes(ref)
        if ref.language == "java":
            scored = self._java(ref, enclosing)
        else:
            scored = self._python(ref, enclosing)
        scored = [(s, tier) for s, tier in scored if tier is not None]
        if not scored:
            return []
        best = max(RANK[tier] for _, tier in scored)
        return [Resolution(s, tier) for s, tier in scored if RANK[tier] == best]

    def find_references(self, target: SymbolRow) -> list[Reference]:
        """Call sites whose best resolution includes target, best tier first."""
        names = {target.name}
        if target.language == "python":
            rows = self.conn.execute(
                "SELECT DISTINCT alias FROM imports WHERE name = ? AND alias IS NOT NULL",
                (target.name,),
            )
            names.update(r["alias"] for r in rows)
        placeholders = ",".join("?" * len(names))
        sql = REF_SELECT + f" WHERE f.language = ? AND (r.name IN ({placeholders})"
        if target.kind == "constructor":
            sql += " OR r.kind = 'constructor_call'"
        sql += ")"
        refs = [ref_row(r) for r in self.conn.execute(sql, [target.language, *names])]

        found = []
        for ref in refs:
            for resolution in self.resolve_ref(ref):
                if resolution.symbol.id == target.id:
                    found.append(Reference(ref, resolution.confidence))
        found.sort(key=lambda r: (-RANK[r.confidence], r.ref.path, r.ref.line, r.ref.col))
        return found

    # --- Python ----------------------------------------------------------------------

    def _python(self, ref: RefRow, enclosing: list[SymbolRow]):
        bindings = self._python_bindings(ref.file_id, ref.module)
        names = {ref.name}
        if ref.receiver is None and ref.name in bindings:
            names.add(self._follow_reexport(bindings[ref.name]).rsplit(".", 1)[-1])
        for name in names:
            for cand in self._named(ref.language, name):
                if cand.kind in _PY_CALLABLE:
                    yield cand, self._python_tier(ref, cand, enclosing, bindings)

    def _python_tier(self, ref, cand, enclosing, bindings) -> str | None:
        parent = self._symbol(cand.parent_id) if cand.parent_id else None
        receiver = ref.receiver

        if parent is None:  # module-level function or class
            if receiver is None:
                bound = bindings.get(ref.name)
                if bound is not None:
                    return EXACT if self._follow_reexport(bound) == cand.qualified_name else None
                if cand.module in self._star_modules(ref.file_id):
                    return LIKELY
                return POSSIBLE
            target = self._expand_python(receiver, bindings)
            if target is not None and self._follow_reexport(target) == cand.module:
                return EXACT
            return None  # an attribute of something that is not cand's module

        if parent.kind in ("function", "method"):  # nested function: only reachable inside
            chain = self._enclosing_chain(ref.enclosing_symbol_id)
            return EXACT if receiver is None and parent.id in chain else None

        # A method (or nested class) of class `parent`.
        if receiver is None:
            return None  # a bare name never reaches a method in Python
        enclosing_ids = {c.id for c in enclosing}
        if receiver in ("self", "cls"):
            if parent.id in enclosing_ids:
                return EXACT
            if any(parent.name in map(simple_name, c.bases) for c in enclosing):
                return LIKELY
            return POSSIBLE
        if receiver.startswith("super("):
            if enclosing and parent.name in map(simple_name, enclosing[0].bases):
                return LIKELY
            return POSSIBLE
        target = self._expand_python(receiver, bindings)
        if target is not None:
            target = self._follow_reexport(target)
            if target == parent.qualified_name:
                return EXACT  # Item.parse(), m.Item.parse()
            other_class = self._type_by_qn(target)
            if other_class is not None:
                return LIKELY if parent.name in map(simple_name, other_class.bases) else None
            if self._is_python_module(target):
                return None
        return LIKELY if self._python_visible(ref, parent, bindings) else POSSIBLE

    def _python_bindings(self, file_id: int, module: str) -> dict[str, str]:
        """Local name -> dotted target for one file: imports, then top-level definitions."""
        if file_id not in self._py_bindings:
            bindings: dict[str, str] = {}
            for imp in self._file_imports(file_id):
                if imp.name == "*":
                    continue
                if imp.name is None:  # import a.b.c [as x]
                    local = imp.alias or imp.module.split(".")[0]
                    bindings[local] = imp.module if imp.alias else local
                else:  # from a import b [as x]
                    bindings[imp.alias or imp.name] = f"{imp.module}.{imp.name}"
            rows = self.conn.execute(
                "SELECT name, qualified_name FROM symbols WHERE file_id = ? AND parent_id IS NULL",
                (file_id,),
            )
            for r in rows:
                bindings[r["name"]] = r["qualified_name"]
            self._py_bindings[file_id] = bindings
        return self._py_bindings[file_id]

    def _expand_python(self, dotted: str, bindings: dict[str, str]) -> str | None:
        """'m.Item' with `import inventory.models as m` -> 'inventory.models.Item'."""
        head, _, rest = dotted.partition(".")
        if head not in bindings or not re.fullmatch(r"[\w.]+", dotted):
            return None
        return bindings[head] + ("." + rest if rest else "")

    def _follow_reexport(self, dotted: str) -> str:
        """One level of re-export: 'inventory.Item' -> 'inventory.models.Item'."""
        if dotted not in self._reexports:
            self._reexports[dotted] = self._lookup_reexport(dotted)
        return self._reexports[dotted]

    def _lookup_reexport(self, dotted: str) -> str:
        if self._by_qualified(dotted) or self._is_python_module(dotted):
            return dotted
        module, _, name = dotted.rpartition(".")
        rows = self.conn.execute(
            "SELECT i.module, i.name FROM imports i JOIN files f ON f.id = i.file_id"
            " WHERE f.module = ? AND f.language = 'python' AND (i.alias = ? OR"
            " (i.alias IS NULL AND i.name = ?))",
            (module, name, name),
        ).fetchall()
        for r in rows:
            if r["name"] and r["name"] != "*":
                return f"{r['module']}.{r['name']}"
        return dotted

    def _python_visible(self, ref: RefRow, cls: SymbolRow, bindings: dict[str, str]) -> bool:
        """Is class `cls` (or its module) reachable by name from the calling file?"""
        if cls.module == ref.module or cls.module in self._star_modules(ref.file_id):
            return True
        for target in bindings.values():
            target = self._follow_reexport(target)
            if target == cls.qualified_name or target == cls.module:
                return True
            if cls.module.startswith(target + "."):  # `import inventory` then inventory.models.X
                return True
        return False

    def _star_modules(self, file_id: int) -> set[str]:
        return {i.module for i in self._file_imports(file_id) if i.name == "*"}

    def _is_python_module(self, dotted: str) -> bool:
        if dotted not in self._py_modules:
            row = self.conn.execute(
                "SELECT 1 FROM files WHERE module = ? AND language = 'python' LIMIT 1", (dotted,)
            ).fetchone()
            self._py_modules[dotted] = row is not None
        return self._py_modules[dotted]

    # --- Java ------------------------------------------------------------------------

    def _java(self, ref: RefRow, enclosing: list[SymbolRow]):
        if ref.kind == "constructor_call":  # this(...) / super(...)
            yield from self._java_constructor_call(ref, enclosing)
            return
        for cand in self._named("java", ref.name):
            if ref.kind == "new":
                if cand.kind in TYPE_KINDS or cand.kind == "constructor":
                    yield cand, self._java_new_tier(ref, cand, enclosing)
            elif self._java_callable(ref, cand):
                yield cand, self._java_member_tier(ref, cand, enclosing)

    def _java_callable(self, ref: RefRow, cand: SymbolRow) -> bool:
        if cand.kind == "method":
            return _arity_fits(ref.arg_count, cand)
        # Records have implicit accessors: item.price() reads component `price`.
        if cand.kind == "field" and ref.arg_count in (0, None) and cand.parent_id:
            parent = self._symbol(cand.parent_id)
            return parent is not None and parent.kind == "record"
        return False

    def _java_member_tier(self, ref, cand, enclosing) -> str | None:
        owner = self._symbol(cand.parent_id)
        if owner is None:
            return POSSIBLE
        receiver = ref.receiver
        enclosing_ids = [c.id for c in enclosing]

        if receiver in (None, "this"):
            if receiver is None and self._static_import_binds(ref, owner):
                return EXACT
            in_scope = enclosing_ids[:1] if receiver == "this" else enclosing_ids
            if owner.id in in_scope:
                return EXACT
            if any(owner.name in map(simple_name, c.bases) for c in enclosing):
                return LIKELY
            return POSSIBLE
        if receiver == "super" or receiver.endswith(".super"):
            if enclosing and owner.name in map(simple_name, enclosing[0].bases):
                return LIKELY
            return POSSIBLE

        named_type = self._java_type(ref, enclosing, receiver)
        if named_type is not None:
            if named_type == owner.qualified_name:
                return EXACT  # Money.round(x), Order::total
            other = self._type_by_qn(named_type)
            if other is not None and owner.name in map(simple_name, other.bases):
                return LIKELY
            return None  # a static call on some other type
        if _EXTERNAL_TYPE.fullmatch(receiver) and not receiver.rsplit(".", 1)[-1].isupper():
            # Written like a type name (Math, java.util.Collections) but not a type we
            # indexed: a JDK/library class, so it cannot be one of our methods.
            return None
        return LIKELY if self._java_visible(ref, enclosing, owner) else POSSIBLE

    def _java_new_tier(self, ref, cand, enclosing) -> str | None:
        if cand.kind == "constructor":
            if not _arity_fits(ref.arg_count, cand):
                return None
            type_qn = self._symbol(cand.parent_id).qualified_name
        else:
            type_qn = cand.qualified_name
        written = f"{ref.receiver}.{ref.name}" if ref.receiver else ref.name
        named_type = self._java_type(ref, enclosing, written)
        if named_type is None:
            return POSSIBLE
        return EXACT if named_type == type_qn else None

    def _java_constructor_call(self, ref: RefRow, enclosing: list[SymbolRow]):
        if not enclosing:
            return
        current = enclosing[0]
        if ref.name == "this":
            owners = [current]
        else:  # super(...)
            owners = []
            for base in current.bases:
                qn = self._java_type(ref, enclosing[1:], simple_name(base))
                owner = self._type_by_qn(qn) if qn else None
                if owner is not None and owner.kind == "class":
                    owners.append(owner)
        for owner in owners:
            for cand in self._named("java", owner.name):
                if (
                    cand.kind == "constructor"
                    and cand.parent_id == owner.id
                    and _arity_fits(ref.arg_count, cand)
                ):
                    yield cand, EXACT

    def _static_import_binds(self, ref: RefRow, owner: SymbolRow) -> bool:
        return any(
            imp.is_static and imp.module == owner.qualified_name and imp.name in (ref.name, "*")
            for imp in self._file_imports(ref.file_id)
        )

    def _java_visible(self, ref, enclosing, owner: SymbolRow) -> bool:
        """Can the calling file name `owner` (or its top-level class) without qualification?"""
        cls = owner
        while cls is not None:
            if self._java_type(ref, enclosing, cls.name) == cls.qualified_name:
                return True
            cls = self._symbol(cls.parent_id) if cls.parent_id else None
        return False

    def _java_type(self, ref: RefRow, enclosing: list[SymbolRow], written: str) -> str | None:
        """Qualified name of the type that `written` denotes in the calling file, if known."""
        key = (ref.file_id, tuple(c.id for c in enclosing), written)
        if key not in self._java_types:
            self._java_types[key] = self._lookup_java_type(ref, enclosing, written)
        return self._java_types[key]

    def _lookup_java_type(self, ref, enclosing, written: str) -> str | None:
        if not re.fullmatch(r"[\w.]+", written):
            return None
        head, _, rest = written.partition(".")
        suffix = "." + rest if rest else ""

        def is_type(qn: str) -> bool:
            return self._type_by_qn(qn) is not None

        # 1. The enclosing classes and their member types.
        for cls in enclosing:
            if cls.name == head and is_type(cls.qualified_name + suffix):
                return cls.qualified_name + suffix
            if is_type(f"{cls.qualified_name}.{head}{suffix}"):
                return f"{cls.qualified_name}.{head}{suffix}"
        # 2. Top-level types in the same file.
        for cand in self._named("java", head):
            if cand.file_id == ref.file_id and cand.parent_id is None and cand.kind in TYPE_KINDS:
                return cand.qualified_name + suffix
        imports = self._file_imports(ref.file_id)
        # 3. Single-type imports.
        for imp in imports:
            if not imp.is_static and imp.name == head:
                return f"{imp.module}.{head}{suffix}"
        # 4. Same package.
        same_package = f"{ref.module}.{head}" if ref.module else head
        if is_type(same_package):
            return same_package + suffix
        # 5. Wildcard imports.
        for imp in imports:
            if not imp.is_static and imp.name == "*" and is_type(f"{imp.module}.{head}"):
                return f"{imp.module}.{head}{suffix}"
        # 6. Fully qualified name written out.
        if is_type(written):
            return written
        return None

    # --- shared helpers ---------------------------------------------------------------

    def _symbol(self, symbol_id: int | None) -> SymbolRow | None:
        if symbol_id is None:
            return None
        if symbol_id not in self._symbols:
            row = self.conn.execute(SYMBOL_SELECT + " WHERE s.id = ?", (symbol_id,)).fetchone()
            self._symbols[symbol_id] = symbol_row(row) if row else None
        return self._symbols[symbol_id]

    def _named(self, language: str, name: str) -> list[SymbolRow]:
        key = (language, name)
        if key not in self._by_name:
            rows = self.conn.execute(
                SYMBOL_SELECT + " WHERE s.name = ? AND f.language = ?", (name, language)
            )
            self._by_name[key] = [symbol_row(r) for r in rows]
            for s in self._by_name[key]:
                self._symbols[s.id] = s
        return self._by_name[key]

    def _by_qualified(self, qualified_name: str) -> list[SymbolRow]:
        if qualified_name not in self._by_qn:
            rows = self.conn.execute(
                SYMBOL_SELECT + " WHERE s.qualified_name = ?", (qualified_name,)
            )
            self._by_qn[qualified_name] = [symbol_row(r) for r in rows]
        return self._by_qn[qualified_name]

    def _type_by_qn(self, qualified_name: str) -> SymbolRow | None:
        types = [s for s in self._by_qualified(qualified_name) if s.kind in TYPE_KINDS]
        return types[0] if types else None

    def _file_imports(self, file_id: int) -> list[ImportRow]:
        if file_id not in self._imports:
            rows = self.conn.execute(
                "SELECT module, name, alias, is_static FROM imports WHERE file_id = ?", (file_id,)
            )
            self._imports[file_id] = [
                ImportRow(r["module"], r["name"], r["alias"], bool(r["is_static"])) for r in rows
            ]
        return self._imports[file_id]

    def _enclosing_chain(self, symbol_id: int | None) -> list[int]:
        chain = []
        while symbol_id is not None:
            chain.append(symbol_id)
            symbol = self._symbol(symbol_id)
            symbol_id = symbol.parent_id if symbol else None
        return chain

    def _enclosing_classes(self, ref: RefRow) -> list[SymbolRow]:
        """Types enclosing the call site, innermost first."""
        classes = []
        for symbol_id in self._enclosing_chain(ref.enclosing_symbol_id):
            symbol = self._symbol(symbol_id)
            if symbol.kind in TYPE_KINDS:
                classes.append(symbol)
        return classes


def _arity_fits(arg_count: int | None, cand: SymbolRow) -> bool:
    """Java overload filter by argument count (varargs aware). Unknown counts always fit."""
    if arg_count is None or cand.param_count is None:
        return True
    if cand.is_varargs:
        return arg_count >= cand.param_count - 1
    return arg_count == cand.param_count
