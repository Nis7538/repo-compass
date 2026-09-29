"""Python extraction on the `inventory` fixture and on small inline snippets."""

import pytest

from compass.indexer.extract_python import extract_python, module_name
from compass.indexer.models import FileExtract
from tests.helpers import FIXTURES

PKG = FIXTURES / "python" / "src" / "inventory"


def _extract(name: str, module: str, is_package: bool = False) -> FileExtract:
    return extract_python((PKG / name).read_bytes(), module, is_package)


def _symbol(out: FileExtract, qualified_name: str, nth: int = 0):
    matches = [s for s in out.symbols if s.qualified_name == qualified_name]
    return matches[nth]


def _qn(out: FileExtract, index: int | None) -> str | None:
    return out.symbols[index].qualified_name if index is not None else None


def _refs(out: FileExtract, name: str):
    return [r for r in out.refs if r.name == name]


@pytest.fixture(scope="module")
def models() -> FileExtract:
    return _extract("models.py", "inventory.models")


@pytest.fixture(scope="module")
def services() -> FileExtract:
    return _extract("services.py", "inventory.services")


def test_models_symbols_in_source_order(models):
    assert [(s.kind, s.qualified_name) for s in models.symbols] == [
        ("class", "inventory.models.Base"),
        ("method", "inventory.models.Base.__init__"),
        ("method", "inventory.models.Base.save"),
        ("class", "inventory.models.Meta"),
        ("class", "inventory.models.Item"),
        ("method", "inventory.models.Item.label"),
        ("method", "inventory.models.Item.label"),
        ("method", "inventory.models.Item.parse"),
        ("method", "inventory.models.Item.empty"),
        ("method", "inventory.models.Item.save"),
        ("method", "inventory.models.Item.validate"),
        ("function", "inventory.models.Item.validate.check"),
        ("class", "inventory.models.Item.History"),
        ("method", "inventory.models.Item.History.record"),
        ("method", "inventory.models.Item.refresh"),
        ("function", "inventory.models.store"),
        ("function", "inventory.models.load"),
        ("function", "inventory.models.dumps"),  # defined inside `except ImportError:`
    ]
    assert not models.has_errors


def test_decorators_are_recorded_without_at_sign(models):
    assert _symbol(models, "inventory.models.Item").decorators == ["dataclass"]
    assert _symbol(models, "inventory.models.Item.label", 0).decorators == ["property"]
    assert _symbol(models, "inventory.models.Item.label", 1).decorators == ["label.setter"]
    assert _symbol(models, "inventory.models.Item.parse").decorators == ["staticmethod"]
    assert _symbol(models, "inventory.models.Item.empty").decorators == ["classmethod"]
    assert _symbol(models, "inventory.models.Item.save").decorators == ["retry(3)"]


def test_decorator_factory_call_is_a_ref_in_the_enclosing_scope(models):
    (retry,) = _refs(models, "retry")
    assert _qn(models, retry.enclosing) == "inventory.models.Item"
    assert retry.arg_count == 1


def test_class_details(models):
    item = _symbol(models, "inventory.models.Item")
    assert item.bases == ["Base"]  # metaclass= is not a base
    assert item.signature == "class Item(Base, metaclass=Meta)"
    assert item.doc == "A stock-keeping unit."
    assert (item.start_line, item.end_line) == (27, 66)  # starts at @dataclass


def test_function_details(models):
    refresh = _symbol(models, "inventory.models.Item.refresh")
    assert refresh.signature == 'async def refresh(self, service: "StockService")'
    assert refresh.param_count == 2
    assert _symbol(models, "inventory.models.Base.save").doc == "Persist the object."
    check = _symbol(models, "inventory.models.Item.validate.check")
    assert _qn(models, check.parent) == "inventory.models.Item.validate"


def test_default_argument_calls_belong_to_the_enclosing_scope(services):
    (empty,) = _refs(services, "empty")
    assert empty.receiver == "m.Item"
    assert empty.enclosing is None  # evaluated at module level, when `build` is defined


def test_call_shapes(services):
    (init,) = _refs(services, "__init__")
    assert init.receiver == "super()"
    assert _qn(services, init.enclosing) == "inventory.services.StockService.__init__"

    saves = {(r.receiver, _qn(services, r.enclosing)) for r in _refs(services, "save")}
    assert saves == {
        ("self.repo", "inventory.services.StockService.add"),
        ("item", "inventory.services.StockService.add"),
        ("utils", "inventory.services.StockService.save"),
    }
    (parse,) = _refs(services, "parse")
    assert (parse.receiver, parse.line, parse.col) == ("m.Item", 22, 23)


def test_calls_in_comprehensions_and_lambdas(services):
    (fmt,) = _refs(services, "fmt")
    (lower,) = _refs(services, "lower")
    assert _qn(services, fmt.enclosing) == "inventory.services.StockService.report"
    assert _qn(services, lower.enclosing) == "inventory.services.StockService.report"
    assert lower.receiver == "i.label"


def test_chained_calls_yield_one_ref_each(services):
    chain = [r for r in services.refs if r.enclosing is not None]
    chain = [r for r in chain if _qn(services, r.enclosing) == "inventory.services.build"]
    assert [(r.name, r.receiver) for r in chain] == [
        ("validate", 'StockService(default).add("x")'),
        ("add", "StockService(default)"),
        ("StockService", None),
    ]


def test_main_guard_call_is_module_level(services):
    (build,) = _refs(services, "build")
    assert build.enclosing is None


def test_import_styles(services):
    assert [(i.module, i.name, i.alias) for i in services.imports] == [
        ("inventory.models", None, "m"),
        ("inventory", "models", None),
        ("inventory", "utils", None),
        ("inventory.helpers", "*", None),
        ("inventory.models", "Base", None),
        ("inventory.models", "Item", "It"),
    ]


def test_imports_inside_blocks_are_found(models):
    modules = [(i.module, i.name) for i in models.imports]
    assert ("inventory.services", "StockService") in modules  # under `if TYPE_CHECKING:`
    assert ("fastjson", "dumps") in modules  # under `try:`


def test_relative_import_from_package_init():
    out = extract_python(b"from .models import Item\nfrom .. import x\n", "inventory", True)
    assert [(i.module, i.name) for i in out.imports] == [
        ("inventory.models", "Item"),
        ("..", "x"),  # climbs above the top-level package: left unresolved
    ]


def test_syntax_errors_are_flagged_but_extraction_continues():
    out = extract_python(b"def ok():\n    pass\n\ndef broken(:\n    pass\n", "m")
    assert out.has_errors
    assert "m.ok" in [s.qualified_name for s in out.symbols]


def test_deeply_nested_expression_does_not_hit_recursion_limit():
    source = ("x = " + " + ".join(["f()"] * 3000) + "\n").encode()
    out = extract_python(source, "m")
    assert len(out.refs) == 3000


def test_splat_arguments_make_arg_count_unknown():
    out = extract_python(b"f(*args)\ng(1, 2)\n", "m")
    assert [r.arg_count for r in out.refs] == [None, 2]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/inventory/models.py", "inventory.models"),
        ("src/inventory/__init__.py", "inventory"),
        ("src/inventory/sub/deep.py", "inventory.sub.deep"),
        ("scripts/seed.py", "seed"),
        ("setup.py", "setup"),
    ],
)
def test_module_name(path, expected):
    package_dirs = {"src/inventory", "src/inventory/sub"}
    assert module_name(path, package_dirs) == expected
