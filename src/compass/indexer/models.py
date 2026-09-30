"""Plain data produced by the extractors and consumed by the store.

Extractors never touch the database. They return a FileExtract whose symbols
refer to each other by list index; the store turns those indexes into row ids.
"""

from dataclasses import dataclass, field

# Symbol kinds that behave like a type (can contain members, can be instantiated).
TYPE_KINDS = frozenset({"class", "interface", "enum", "record", "annotation"})


@dataclass
class Symbol:
    kind: str  # class|interface|enum|record|annotation|method|constructor|function|field
    name: str
    qualified_name: str
    signature: str
    start_line: int
    end_line: int
    parent: int | None = None  # index into FileExtract.symbols; parents come before children
    param_count: int | None = None
    is_varargs: bool = False
    decorators: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    doc: str | None = None


@dataclass
class Import:
    module: str  # normalized: 'java.util', 'inventory.models' (relative imports resolved)
    name: str | None  # imported member, '*' for wildcard, None for `import a.b`
    alias: str | None
    line: int
    is_static: bool = False
    type_only: bool = False  # Python: inside `if TYPE_CHECKING:`, never imported at runtime


@dataclass
class Ref:
    kind: str  # call | new | method_ref | constructor_call
    name: str  # callee simple name: 'add', 'Order' for `new Order()`, 'super' for `super(...)`
    receiver: str | None  # raw receiver text: None, 'self', 'this', 'm', 'a.b()'
    arg_count: int | None
    line: int  # 1-based
    col: int  # 1-based
    enclosing: int | None  # index into FileExtract.symbols; None means module/file level


@dataclass
class FileExtract:
    module: str  # Java package or Python dotted module name
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    has_errors: bool = False
