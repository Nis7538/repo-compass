"""Extract symbols, imports and call sites from Java source.

Same shape as extract_python: an iterative walk with an explicit stack whose
entries carry the enclosing symbol index. A third flag marks anonymous class
bodies (`new Runnable() { ... }`, enum constant bodies): declarations inside them
are not indexed as symbols, but their call sites are, attributed to the
enclosing named method.
"""

from tree_sitter import Node

from compass.indexer.models import FileExtract, Import, Ref, Symbol
from compass.indexer.treesitter import (
    MAX_DOC,
    MAX_RECEIVER,
    MAX_SIGNATURE,
    col,
    end_line,
    line,
    one_line,
    parser_for,
    text,
)

_TYPE_KINDS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "record_declaration": "record",
    "annotation_type_declaration": "annotation",
}
_METHOD_KINDS = {
    "method_declaration": "method",
    "annotation_type_element_declaration": "method",
    "constructor_declaration": "constructor",
    "compact_constructor_declaration": "constructor",
}
_FIELD_DECLARATIONS = ("field_declaration", "constant_declaration")
_ANNOTATIONS = ("marker_annotation", "annotation")
_COMMENTS = ("line_comment", "block_comment")


def extract_java(source: bytes) -> FileExtract:
    tree = parser_for("java").parse(source)
    root = tree.root_node
    package = ""
    for child in root.named_children:
        if child.type == "package_declaration":
            package = next(
                text(c) for c in child.named_children if c.type not in _ANNOTATIONS + _COMMENTS
            )
            break
    out = FileExtract(module=package, has_errors=root.has_error)
    _Walker(out).run(root)
    return out


class _Walker:
    def __init__(self, out: FileExtract):
        self.out = out
        self.record_arity: dict[int, int] = {}  # record symbol index -> component count

    def run(self, root: Node) -> None:
        # Entries: (node, enclosing symbol index, inside an anonymous class body?)
        stack: list[tuple[Node, int | None, bool]] = [(root, None, False)]
        while stack:
            node, scope, anonymous = stack.pop()
            kind = node.type
            children = node.named_children
            if not anonymous:
                if kind in _TYPE_KINDS:
                    scope = self._type(node, scope)
                elif kind in _METHOD_KINDS:
                    scope = self._method(node, scope)
                elif kind in _FIELD_DECLARATIONS:
                    # Initializer calls belong to the field they initialize.
                    for declarator, field_index in reversed(self._fields(node, scope)):
                        stack.append((declarator, field_index, False))
                    continue
                elif kind == "enum_constant":
                    self._enum_constant(node, scope)

            if kind == "import_declaration":
                self._import(node)
                continue
            if kind == "method_invocation":
                self._invocation(node, scope)
            elif kind == "object_creation_expression":
                self._creation(node, scope)
            elif kind == "method_reference":
                self._method_reference(node, scope)
            elif kind == "explicit_constructor_invocation":
                self._constructor_call(node, scope)

            for child in reversed(children):
                # Bodies of anonymous classes: `new X() { ... }` and `A(1) { ... }` constants.
                starts_anonymous = child.type == "class_body" and kind in (
                    "object_creation_expression",
                    "enum_constant",
                )
                stack.append((child, scope, anonymous or starts_anonymous))

    # --- definitions -------------------------------------------------------------

    def _add_symbol(self, symbol: Symbol) -> int:
        self.out.symbols.append(symbol)
        return len(self.out.symbols) - 1

    def _qualify(self, scope: int | None, name: str) -> str:
        prefix = self.out.symbols[scope].qualified_name if scope is not None else self.out.module
        return f"{prefix}.{name}" if prefix else name

    def _type(self, node: Node, scope: int | None) -> int:
        name = text(node.child_by_field_name("name"))
        bases = []
        for child in node.named_children:
            if child.type == "superclass":
                bases.extend(_type_name(c) for c in child.named_children)
            elif child.type in ("super_interfaces", "extends_interfaces"):
                for type_list in child.named_children:
                    bases.extend(_type_name(c) for c in type_list.named_children)
        index = self._add_symbol(
            Symbol(
                kind=_TYPE_KINDS[node.type],
                name=name,
                qualified_name=self._qualify(scope, name),
                signature=_header(node, node.child_by_field_name("body")),
                start_line=line(node),
                end_line=end_line(node),
                parent=scope,
                decorators=_annotations(node),
                bases=bases,
                doc=_javadoc(node),
            )
        )
        if node.type == "record_declaration":
            self._record_components(node.child_by_field_name("parameters"), index)
        return index

    def _record_components(self, params: Node, scope: int) -> None:
        """`record Item(String sku, double price)` declares fields sku and price."""
        components = [p for p in params.named_children if p.type not in _COMMENTS]
        self.record_arity[scope] = len(components)
        for component in components:
            name_node = component.child_by_field_name("name")
            if name_node is None:  # varargs component: `String... tags`
                name_node = component.named_children[-1].child_by_field_name("name")
            name = text(name_node)
            self._add_symbol(
                Symbol(
                    kind="field",
                    name=name,
                    qualified_name=self._qualify(scope, name),
                    signature=one_line(text(component), MAX_SIGNATURE),
                    start_line=line(component),
                    end_line=end_line(component),
                    parent=scope,
                )
            )

    def _method(self, node: Node, scope: int | None) -> int:
        name = text(node.child_by_field_name("name"))
        params = node.child_by_field_name("parameters")
        param_count, is_varargs = None, False
        if params is not None:
            param_nodes = [p for p in params.named_children if p.type not in _COMMENTS]
            param_nodes = [p for p in param_nodes if p.type != "receiver_parameter"]
            param_count = len(param_nodes)
            is_varargs = any(p.type == "spread_parameter" for p in param_nodes)
        elif node.type == "annotation_type_element_declaration":
            param_count = 0
        elif node.type == "compact_constructor_declaration":
            # A record's compact constructor takes the record components.
            param_count = self.record_arity.get(scope)
        return self._add_symbol(
            Symbol(
                kind=_METHOD_KINDS[node.type],
                name=name,
                qualified_name=self._qualify(scope, name),
                signature=_header(node, node.child_by_field_name("body")),
                start_line=line(node),
                end_line=end_line(node),
                parent=scope,
                param_count=param_count,
                is_varargs=is_varargs,
                decorators=_annotations(node),
                doc=_javadoc(node),
            )
        )

    def _fields(self, node: Node, scope: int | None) -> list[tuple[Node, int]]:
        """One field symbol per declarator: `int a, b;` gives two fields."""
        prefix = _modifier_words(node) + [text(node.child_by_field_name("type"))]
        added = []
        for declarator in node.children_by_field_name("declarator"):
            name = text(declarator.child_by_field_name("name"))
            index = self._add_symbol(
                Symbol(
                    kind="field",
                    name=name,
                    qualified_name=self._qualify(scope, name),
                    signature=one_line(" ".join([*prefix, name]), MAX_SIGNATURE),
                    start_line=line(declarator),
                    end_line=end_line(declarator),
                    parent=scope,
                    decorators=_annotations(node),
                    doc=_javadoc(node),
                )
            )
            added.append((declarator, index))
        return added

    def _enum_constant(self, node: Node, scope: int | None) -> None:
        name = text(node.child_by_field_name("name"))
        self._add_symbol(
            Symbol(
                kind="field",
                name=name,
                qualified_name=self._qualify(scope, name),
                signature=name,
                start_line=line(node),
                end_line=end_line(node),
                parent=scope,
                decorators=_annotations(node),
                doc=_javadoc(node),
            )
        )
        arguments = node.child_by_field_name("arguments")
        if arguments is not None and scope is not None:
            # `A(1)` invokes the enum's constructor.
            self._add_ref("new", self.out.symbols[scope].name, None, arguments, node, scope)

    # --- references --------------------------------------------------------------

    def _add_ref(self, kind, name, receiver, arguments, position: Node, scope) -> None:
        self.out.refs.append(
            Ref(
                kind=kind,
                name=name,
                receiver=one_line(receiver, MAX_RECEIVER) if receiver else None,
                arg_count=_arg_count(arguments),
                line=line(position),
                col=col(position),
                enclosing=scope,
            )
        )

    def _invocation(self, node: Node, scope) -> None:
        name = node.child_by_field_name("name")
        receiver_parts = []
        obj = node.child_by_field_name("object")
        if obj is not None:
            receiver_parts.append(text(obj))
        if any(c.type == "super" and c != obj for c in node.named_children):
            receiver_parts.append("super")  # Outer.super.m()
        receiver = ".".join(receiver_parts) or None
        self._add_ref(
            "call", text(name), receiver, node.child_by_field_name("arguments"), name, scope
        )

    def _creation(self, node: Node, scope) -> None:
        type_node = node.child_by_field_name("type")
        qualifier, _, name = _type_name(type_node).rpartition(".")
        self._add_ref(
            "new", name, qualifier, node.child_by_field_name("arguments"), type_node, scope
        )

    def _method_reference(self, node: Node, scope) -> None:
        # `Order::total`, `this::m`, `Order.Line::new`
        target = node.named_children[0]
        last = node.children[-1]
        if last.type == "new":
            qualifier, _, name = _type_name(target).rpartition(".")
            self._add_ref("new", name, qualifier, None, target, scope)
        else:
            self._add_ref("method_ref", text(last), text(target), None, last, scope)

    def _constructor_call(self, node: Node, scope) -> None:
        # `super(x)` / `this(x)` inside a constructor.
        constructor = node.child_by_field_name("constructor")
        self._add_ref(
            "constructor_call",
            text(constructor),
            None,
            node.child_by_field_name("arguments"),
            constructor,
            scope,
        )

    # --- imports -----------------------------------------------------------------

    def _import(self, node: Node) -> None:
        is_static = any(c.type == "static" for c in node.children)
        wildcard = any(c.type == "asterisk" for c in node.named_children)
        path = next(
            text(c) for c in node.named_children if c.type in ("scoped_identifier", "identifier")
        )
        if wildcard:
            module, name = path, "*"
        else:
            module, _, name = path.rpartition(".")
        self.out.imports.append(Import(module, name, None, line(node), is_static))


def _type_name(node: Node) -> str:
    """Type text without generic arguments: `Map<K, V>` -> 'Map', `a.B<T>` -> 'a.B'."""
    if node.type == "generic_type":
        node = node.named_children[0]
    return one_line(text(node), MAX_SIGNATURE).replace(" ", "")


def _modifiers(node: Node) -> Node | None:
    return next((c for c in node.named_children if c.type == "modifiers"), None)


def _modifier_words(node: Node) -> list[str]:
    modifiers = _modifiers(node)
    if modifiers is None:
        return []
    return [text(c) for c in modifiers.children if c.type not in _ANNOTATIONS + _COMMENTS]


def _annotations(node: Node) -> list[str]:
    modifiers = _modifiers(node)
    if modifiers is None:
        return []
    return [
        one_line(text(c).removeprefix("@"), MAX_SIGNATURE)
        for c in modifiers.named_children
        if c.type in _ANNOTATIONS
    ]


def _header(node: Node, body: Node | None) -> str:
    """Declaration text up to the body, keeping modifier keywords but not annotations."""
    modifiers = _modifiers(node)
    start = modifiers.end_byte if modifiers is not None else node.start_byte
    end = body.start_byte if body is not None else node.end_byte
    rest = node.text[start - node.start_byte : end - node.start_byte]
    rest = rest.decode("utf-8", errors="replace").strip().removesuffix(";")
    return one_line(" ".join([*_modifier_words(node), rest]), MAX_SIGNATURE)


def _javadoc(node: Node) -> str | None:
    previous = node.prev_named_sibling
    if previous is None or previous.type != "block_comment":
        return None
    raw = text(previous)
    if not raw.startswith("/**"):
        return None
    lines = raw.removeprefix("/**").removesuffix("*/").splitlines()
    cleaned = [ln.strip().removeprefix("*").strip() for ln in lines]
    return "\n".join(ln for ln in cleaned if ln)[:MAX_DOC] or None


def _arg_count(arguments: Node | None) -> int | None:
    if arguments is None:
        return None
    return sum(1 for a in arguments.named_children if a.type not in _COMMENTS)
