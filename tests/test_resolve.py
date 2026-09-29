"""Reference resolution on the fixtures: expected targets and confidence tiers.

Tests named test_limit_* pin known imprecision documented in
docs/adr/003-reference-resolution.md. If one starts failing because resolution
got smarter, update the ADR along with the test.
"""

import pytest

from compass.indexer.pipeline import index_repo
from compass.store.db import open_index
from compass.store.queries import REF_SELECT, SymbolRow, find_symbols, ref_row
from compass.store.resolve import Resolver
from tests.helpers import FIXTURES

J = "com.example.shop."
P = "inventory."


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    db = tmp_path_factory.mktemp("resolve") / "index.db"
    index_repo(FIXTURES, db)
    conn = open_index(db)
    yield conn
    conn.close()


@pytest.fixture
def resolver(conn):
    return Resolver(conn)


def _ref(conn, file: str, name: str, receiver: str | None, arg_count=..., kind=None):
    sql = REF_SELECT + " WHERE f.path LIKE ? AND r.name = ? AND r.receiver IS ?"
    args = ["%/" + file, name, receiver]
    if arg_count is not ...:
        sql += " AND r.arg_count IS ?"
        args.append(arg_count)
    if kind:
        sql += " AND r.kind = ?"
        args.append(kind)
    rows = conn.execute(sql + " ORDER BY r.line", args).fetchall()
    assert rows, f"no ref {receiver}.{name} in {file}"
    return ref_row(rows[0])


def _label(symbol: SymbolRow) -> str:
    """Qualified name, plus /arity for Java methods and constructors (overloads)."""
    qn = symbol.qualified_name
    if symbol.language == "java" and symbol.kind in ("method", "constructor"):
        qn += f"/{symbol.param_count}"
    return qn


def _resolve(resolver, conn, *ref_args, **ref_kwargs) -> set[tuple[str, str]]:
    ref = _ref(conn, *ref_args, **ref_kwargs)
    return {(_label(r.symbol), r.confidence) for r in resolver.resolve_ref(ref)}


def _symbol(conn, qualified_name: str, param_count=...) -> SymbolRow:
    matches = find_symbols(conn, qualified_name)
    if param_count is not ...:
        matches = [s for s in matches if s.param_count == param_count]
    matches = [s for s in matches if s.qualified_name == qualified_name]
    assert len(matches) == 1, matches
    return matches[0]


# --- Python ---------------------------------------------------------------------------

PYTHON_CASES = [
    # (file, callee, receiver) -> {(target, tier)}
    (("services.py", "It", None), {(P + "models.Item", "exact")}),  # import alias
    (("services.py", "parse", "m.Item"), {(P + "models.Item.parse", "exact")}),  # module alias
    (("services.py", "load", "models"), {(P + "models.load", "exact")}),  # from pkg import mod
    (("services.py", "save", "utils"), {(P + "utils.save", "exact")}),  # from . import mod
    (("services.py", "empty", "m.Item"), {(P + "models.Item.empty", "exact")}),
    (("services.py", "StockService", None), {(P + "services.StockService", "exact")}),
    (("services.py", "build", None), {(P + "services.build", "exact")}),
    (("services.py", "fmt", None), {(P + "helpers.fmt", "likely")}),  # star import
    (("services.py", "__init__", "super()"), {(P + "models.Base.__init__", "likely")}),
    (("models.py", "validate", "self"), {(P + "models.Item.validate", "exact")}),
    (("models.py", "save", "super()"), {(P + "models.Base.save", "likely")}),
    (("models.py", "store", None), {(P + "models.store", "exact")}),
    (("models.py", "check", None), {(P + "models.Item.validate.check", "exact")}),
    (("models.py", "retry", None), {(P + "helpers.retry", "exact")}),  # decorator factory
    (("models.py", "Item", None), {(P + "models.Item", "exact")}),
    (("models.py", "reload", "service"), {(P + "services.StockService.reload", "likely")}),
    (("seed.py", "persist", None), {(P + "models.store", "exact")}),  # from x import a as b
    (("seed.py", "load", None), {(P + "models.load", "exact")}),
    (("models.py", "upper", "self.sku"), set()),  # builtin str method: nothing indexed
]


@pytest.mark.parametrize(("ref", "expected"), PYTHON_CASES, ids=lambda v: str(v))
def test_python_resolution(resolver, conn, ref, expected):
    assert _resolve(resolver, conn, *ref) == expected


# --- Java -----------------------------------------------------------------------------

JAVA_CASES = [
    # (file, callee, receiver, arg_count) -> {(target, tier)}
    (("OrderService.java", "round", None, 1), {(J + "util.Money.round/1", "exact")}),  # static
    (("Money.java", "round", None, 2), {(J + "util.Money.round/2", "exact")}),  # overload
    (("OrderService.java", "add", "order", 1), {(J + "model.Order.add/1", "likely")}),
    (("OrderService.java", "add", "order", 2), {(J + "model.Order.add/2", "likely")}),
    (("OrderService.java", "audit", "this", 1), {(J + "service.OrderService.audit/1", "exact")}),
    (("OrderService.java", "helper", None, 0), {(J + "service.OrderService.helper/0", "exact")}),
    (
        ("OrderService.java", "validate", "super", 0),
        {(J + "service.BaseService.validate/0", "likely")},
    ),  # noqa: E501
    (("OrderService.java", "log", None, 1), {(J + "service.BaseService.log/1", "likely")}),
    (("Order.java", "add", None, 2), {(J + "model.Order.add/2", "exact")}),
    (("Order.java", "add", None, 1), {(J + "model.Order.add/1", "exact")}),  # from varargs loop
    (("Order.java", "validate", None, 0), {(J + "model.Order.validate/0", "exact")}),  # inner
    (("Order.java", "subtotal", "line", 0), {(J + "model.Order.Line.subtotal/0", "likely")}),
    (("Order.java", "price", "item", 0), {(J + "model.Item.price", "likely")}),  # record
    (("Order.java", "this", None, 1), {(J + "model.Order.Order/1", "exact")}),  # this(0)
    (("Money.java", "round", "Math", 1), set()),  # JDK class, not ours
    (("OrderService.java", "valueOf", "String", 1), set()),
]


@pytest.mark.parametrize(("ref", "expected"), JAVA_CASES, ids=lambda v: str(v))
def test_java_resolution(resolver, conn, ref, expected):
    assert _resolve(resolver, conn, *ref) == expected


def test_java_method_reference_is_exact(resolver, conn):
    got = _resolve(resolver, conn, "OrderService.java", "total", "Order", kind="method_ref")
    assert got == {(J + "model.Order.total/0", "exact")}


def test_java_new_resolves_class_and_matching_constructor(resolver, conn):
    got = _resolve(resolver, conn, "OrderService.java", "Line", "Order", kind="new")
    assert got == {(J + "model.Order.Line", "exact"), (J + "model.Order.Line.Line/2", "exact")}
    got = _resolve(resolver, conn, "Order.java", "Line", None, kind="new")  # nested, unqualified
    assert got == {(J + "model.Order.Line", "exact"), (J + "model.Order.Line.Line/2", "exact")}


def test_java_enum_constants_call_the_enum_constructor(resolver, conn):
    got = _resolve(resolver, conn, "Status.java", "Status", None, kind="new")
    assert got == {(J + "model.Status", "exact"), (J + "model.Status.Status/1", "exact")}


# --- find_references ------------------------------------------------------------------


def _callers(resolver, target: SymbolRow) -> list[tuple[str, int, str]]:
    return [
        (r.ref.path.rsplit("/", 1)[-1], r.ref.line, r.confidence)
        for r in resolver.find_references(target)
    ]


def test_find_references_exact_before_likely(resolver, conn):
    add1 = _symbol(conn, J + "model.Order.add", param_count=1)
    assert _callers(resolver, add1) == [
        ("Order.java", 36, "exact"),  # add(item) inside addAll
        ("Order.java", 30, "likely"),  # lines.add(...) -- see test_limit_collection_calls
        ("OrderService.java", 17, "likely"),  # order.add(item)
    ]


def test_find_references_respects_overloads(resolver, conn):
    round1 = _symbol(conn, J + "util.Money.round", param_count=1)
    round2 = _symbol(conn, J + "util.Money.round", param_count=2)
    assert _callers(resolver, round1) == [("OrderService.java", 30, "exact")]
    assert _callers(resolver, round2) == [("Money.java", 7, "exact")]


def test_find_references_through_import_alias(resolver, conn):
    store = _symbol(conn, P + "models.store")
    assert _callers(resolver, store) == [("seed.py", 6, "exact"), ("models.py", 20, "exact")]
    item = _symbol(conn, P + "models.Item")
    assert _callers(resolver, item) == [("models.py", 44, "exact"), ("services.py", 18, "exact")]


def test_find_references_constructor_includes_this_call_and_method_ref(resolver, conn):
    ctor = _symbol(conn, J + "model.Order.Order", param_count=1)
    assert _callers(resolver, ctor) == [
        ("Order.java", 17, "exact"),  # this(0)
        ("OrderService.java", 12, "exact"),  # Order::new (arity unknown)
    ]


def test_distractors_get_no_callers(resolver, conn):
    assert _callers(resolver, _symbol(conn, J + "other.Report.total")) == []
    assert _callers(resolver, _symbol(conn, J + "other.Report.add")) == []
    utils_save = _symbol(conn, P + "utils.save")
    assert _callers(resolver, utils_save) == [("services.py", 28, "exact")]


# --- pinned limitations ---------------------------------------------------------------


def test_limit_no_type_inference_python(resolver, conn):
    """`item = It(sku); item.save()` -- the local's type is not tracked."""
    assert _resolve(resolver, conn, "services.py", "save", "item") == {
        (P + "models.Base.save", "likely"),
        (P + "models.Item.save", "likely"),
        (P + "services.StockService.save", "likely"),
    }


def test_limit_collection_calls(resolver, conn):
    """`lines` is a List field; `lines.add(...)` still matches Order.add as likely."""
    got = _resolve(resolver, conn, "Order.java", "add", "lines", 1)
    assert got == {(J + "model.Order.add/1", "likely")}


def test_limit_interface_call_not_linked_to_implementations(resolver, conn):
    """total() inside Priced resolves to Priced.total only, never to Order.total."""
    got = _resolve(resolver, conn, "Priced.java", "total", None, 0)
    assert got == {(J + "model.Priced.total/0", "exact")}


def test_limit_ambiguous_receiver_lists_every_visible_candidate(resolver, conn):
    got = _resolve(resolver, conn, "OrderService.java", "total", "order", 0)
    assert got == {(J + "model.Order.total/0", "likely"), (J + "model.Priced.total/0", "likely")}


def test_limit_cls_call_is_not_linked_to_the_class(resolver, conn):
    assert _resolve(resolver, conn, "models.py", "cls", None) == set()


# --- symbol lookup --------------------------------------------------------------------


def test_find_symbols_exact_name_ordered_by_location(conn):
    assert [s.qualified_name for s in find_symbols(conn, "save")] == [
        P + "models.Base.save",
        P + "models.Item.save",
        P + "services.StockService.save",
        P + "utils.save",
    ]


def test_find_symbols_qualified_suffix_and_kind(conn):
    assert [s.qualified_name for s in find_symbols(conn, "Order.add")] == [
        J + "model.Order.add",
        J + "model.Order.add",
    ]
    assert [s.kind for s in find_symbols(conn, "Order", kind="constructor")] == [
        "constructor",
        "constructor",
    ]


def test_find_symbols_falls_back_to_word_prefix_search(conn):
    assert {s.name for s in find_symbols(conn, "subtot")} == {"subtotal"}
    # camelCase query words match snake/camel name words: 'stockServ' -> 'stock service'
    assert find_symbols(conn, "stockServ")[0].name == "StockService"
    assert find_symbols(conn, "stock")[0].name == "StockService"  # best-ranked first
    assert find_symbols(conn, "zzz") == []
