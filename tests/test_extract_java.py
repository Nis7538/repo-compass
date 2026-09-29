"""Java extraction on the `shop` fixture and on small inline snippets."""

import pytest

from compass.indexer.extract_java import extract_java
from compass.indexer.models import FileExtract
from tests.helpers import FIXTURES

SRC = FIXTURES / "java" / "shop" / "src" / "main" / "java" / "com" / "example" / "shop"
PKG = "com.example.shop"


def _extract(rel: str) -> FileExtract:
    return extract_java((SRC / rel).read_bytes())


def _symbols(out: FileExtract, qualified_name: str):
    return [s for s in out.symbols if s.qualified_name == qualified_name]


def _qn(out: FileExtract, index: int | None) -> str | None:
    return out.symbols[index].qualified_name if index is not None else None


def _refs(out: FileExtract, name: str):
    return [r for r in out.refs if r.name == name]


@pytest.fixture(scope="module")
def order() -> FileExtract:
    return _extract("model/Order.java")


@pytest.fixture(scope="module")
def service() -> FileExtract:
    return _extract("service/OrderService.java")


def test_package_and_imports(order, service):
    assert order.module == f"{PKG}.model"
    assert [(i.module, i.name, i.is_static) for i in service.imports] == [
        (f"{PKG}.util.Money", "round", True),
        (f"{PKG}.model", "*", False),
        (f"{PKG}.model.Order", "Line", False),
        ("java.util", "List", False),
        ("java.util.function", "Supplier", False),
    ]


def test_overloads_are_distinct_symbols(order):
    adds = _symbols(order, f"{PKG}.model.Order.add")
    assert [(s.signature, s.param_count) for s in adds] == [
        ("public Order add(Item item)", 1),
        ("public Order add(Item item, int quantity)", 2),
    ]
    constructors = _symbols(order, f"{PKG}.model.Order.Order")
    assert [(s.kind, s.param_count) for s in constructors] == [
        ("constructor", 0),
        ("constructor", 1),
    ]


def test_varargs_and_generic_method(order):
    (add_all,) = _symbols(order, f"{PKG}.model.Order.addAll")
    assert (add_all.param_count, add_all.is_varargs) == (1, True)
    (largest,) = _symbols(order, f"{PKG}.model.Order.largest")
    assert largest.signature == "public <T extends Comparable<T>> T largest(List<T> values)"


def test_one_field_per_declarator(order):
    fields = [s.name for s in order.symbols if s.kind == "field" and s.parent == 0]
    assert fields == ["lines", "discount", "shipping", "status"]
    (shipping,) = _symbols(order, f"{PKG}.model.Order.shipping")
    assert shipping.signature == "private int shipping"  # initializer is not part of it


def test_nested_inner_and_enum_classes(order):
    (line_cls,) = _symbols(order, f"{PKG}.model.Order.Line")
    assert (line_cls.kind, line_cls.signature) == ("class", "public static class Line")
    assert line_cls.doc == "A single order line."
    (subtotal,) = _symbols(order, f"{PKG}.model.Order.Line.subtotal")
    assert _qn(order, subtotal.parent) == f"{PKG}.model.Order.Line"
    (track,) = _symbols(order, f"{PKG}.model.Order.Tracker.track")
    assert track.kind == "method"
    (channel,) = _symbols(order, f"{PKG}.model.Order.Channel")
    assert channel.kind == "enum"
    assert _symbols(order, f"{PKG}.model.Order.Channel.WEB")[0].kind == "field"


def test_annotations_javadoc_and_bases(order, service):
    (cls,) = _symbols(order, f"{PKG}.model.Order")
    assert cls.decorators == ["Audited"]
    assert cls.bases == ["Priced"]
    assert cls.doc == "An order made of lines."
    assert cls.signature == "public class Order implements Priced"  # no annotation text
    assert _symbols(order, f"{PKG}.model.Order.total")[0].decorators == ["Override"]
    assert _symbols(order, f"{PKG}.model.Order.add")[0].doc == "Add one item."
    assert service.symbols[0].bases == ["BaseService"]


def test_record_components_and_compact_constructor():
    out = _extract("model/Item.java")
    assert [(s.kind, s.name) for s in out.symbols] == [
        ("record", "Item"),
        ("field", "sku"),
        ("field", "price"),
        ("constructor", "Item"),
    ]
    assert out.symbols[3].param_count == 2


def test_enum_with_constant_body_and_constructor():
    out = _extract("model/Status.java")
    names = [(s.kind, s.name) for s in out.symbols]
    assert names == [
        ("enum", "Status"),
        ("field", "OPEN"),
        ("field", "PAID"),
        ("field", "SHIPPED"),
        ("field", "code"),
        ("constructor", "Status"),
        ("method", "label"),  # PAID's overriding label() is in an anonymous body: not a symbol
    ]
    # Each constant with arguments calls the constructor.
    assert [(r.kind, r.name, r.arg_count) for r in out.refs] == [("new", "Status", 1)] * 3


def test_annotation_type():
    out = _extract("model/Audited.java")
    assert [(s.kind, s.name, s.param_count) for s in out.symbols] == [
        ("annotation", "Audited", None),
        ("method", "value", 0),
    ]
    assert out.symbols[0].decorators == ["Retention(RetentionPolicy.RUNTIME)"]


def test_call_receivers(service):
    shapes = {(r.name, r.receiver, r.arg_count) for r in service.refs if r.kind == "call"}
    assert ("add", "order", 1) in shapes
    assert ("add", "order", 2) in shapes
    assert ("validate", "super", 0) in shapes
    assert ("audit", "this", 1) in shapes
    assert ("round", None, 1) in shapes  # statically imported
    assert ("validate", None, 0) in shapes


def test_method_references_and_creation(service):
    (total_ref,) = [r for r in service.refs if r.kind == "method_ref"]
    assert (total_ref.name, total_ref.receiver) == ("total", "Order")
    news = [(r.name, r.receiver, r.arg_count) for r in service.refs if r.kind == "new"]
    assert news == [
        ("Order", None, None),  # Order::new
        ("Thread", None, 1),
        ("Runnable", None, 0),
        ("Line", "Order", 2),  # new Order.Line(item, 1)
    ]


def test_anonymous_class_calls_belong_to_enclosing_method(service):
    (helper_call,) = _refs(service, "helper")
    assert _qn(service, helper_call.enclosing) == f"{PKG}.service.OrderService.checkout"
    names = [s.name for s in service.symbols]
    assert "run" not in names  # the anonymous Runnable.run is not a symbol


def test_lambda_calls_belong_to_enclosing_method(service):
    log_calls = {_qn(service, r.enclosing) for r in _refs(service, "log")}
    assert log_calls == {
        f"{PKG}.service.OrderService.checkout",
        f"{PKG}.service.OrderService.audit",
    }


def test_field_initializer_refs_belong_to_the_field(service):
    (ctor_ref,) = [r for r in service.refs if r.kind == "new" and r.name == "Order"]
    assert _qn(service, ctor_ref.enclosing) == f"{PKG}.service.OrderService.factory"


def test_explicit_constructor_invocation(order):
    (this_call,) = [r for r in order.refs if r.kind == "constructor_call"]
    assert (this_call.name, this_call.arg_count) == ("this", 1)


def test_syntax_error_is_flagged_but_rest_is_extracted():
    out = _extract("broken/Broken.java")
    assert out.has_errors
    assert [s.name for s in out.symbols] == ["Broken", "ok", "broken"]
    assert _refs(out, "println")


def test_file_without_package_and_local_class():
    source = b"class A { void m() { class Local { void n() { m(); } } } }"
    out = extract_java(source)
    assert out.module == ""
    assert [s.qualified_name for s in out.symbols] == ["A", "A.m", "A.m.Local", "A.m.Local.n"]


def test_deeply_nested_expression_does_not_hit_recursion_limit():
    expr = " + ".join(["f()"] * 3000)
    out = extract_java(f"class A {{ int x = {expr}; }}".encode())
    assert len(out.refs) == 3000
