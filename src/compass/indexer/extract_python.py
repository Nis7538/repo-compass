"""Extract symbols, imports and call sites from Python source.

The walk is iterative (an explicit stack) rather than recursive so deeply
nested expressions cannot hit Python's recursion limit. Each stack entry
carries the index of the enclosing symbol, which becomes a ref's `enclosing`.
"""

import ast
import inspect
from pathlib import PurePosixPath

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

_SPLATS = ("list_splat", "dictionary_splat")


def module_name(rel_path: str, package_dirs: set[str]) -> str:
    """Dotted module name for a repo-relative .py path.

    Walks up from the file while the directory is a package (has __init__.py),
    so `src/inventory/models.py` becomes `inventory.models` when `src/` is not a
    package. package_dirs holds repo-relative directory paths ('.' for the root).
    Namespace packages (PEP 420, no __init__.py) are not recognized.
    """
    path = PurePosixPath(rel_path)
    parts = [] if path.name == "__init__.py" else [path.stem]
    directory = path.parent
    while directory.name and str(directory) in package_dirs:
        parts.insert(0, directory.name)
        directory = directory.parent
    if not parts:  # an __init__.py whose directory is the repo root
        parts = [path.parent.name or path.stem]
    return ".".join(parts)


def extract_python(source: bytes, module: str, is_package: bool = False) -> FileExtract:
    tree = parser_for("python").parse(source)
    out = FileExtract(module=module, has_errors=tree.root_node.has_error)
    _Walker(out, is_package).run(tree.root_node)
    return out


class _Walker:
    def __init__(self, out: FileExtract, is_package: bool):
        self.out = out
        # Package that relative imports are resolved against.
        self.package = out.module if is_package else out.module.rpartition(".")[0]

    def run(self, root: Node) -> None:
        # Entries: (node, enclosing symbol index, decorator nodes for a definition)
        stack: list[tuple[Node, int | None, list[Node]]] = [(root, None, [])]
        while stack:
            node, scope, decorators = stack.pop()
            kind = node.type
            children = node.named_children
            if kind == "decorated_definition":
                decorators = [c for c in children if c.type == "decorator"]
                definition = node.child_by_field_name("definition")
                # Decorator expressions run in the enclosing scope.
                stack.append((definition, scope, decorators))
                stack.extend((d, scope, []) for d in reversed(decorators))
                continue
            if kind == "function_definition":
                self._function(node, scope, decorators, stack)
                continue
            if kind == "class_definition":
                self._class(node, scope, decorators, stack)
                continue
            if kind == "call":
                self._call(node, scope)
            elif kind == "import_statement":
                self._import(node)
            elif kind == "import_from_statement":
                self._import_from(node)
            stack.extend((c, scope, []) for c in reversed(children))

    # --- definitions -------------------------------------------------------------

    def _add_symbol(self, symbol: Symbol) -> int:
        self.out.symbols.append(symbol)
        return len(self.out.symbols) - 1

    def _qualify(self, scope: int | None, name: str) -> str:
        prefix = self.out.symbols[scope].qualified_name if scope is not None else self.out.module
        return f"{prefix}.{name}" if prefix else name

    def _function(self, node: Node, scope, decorators, stack) -> None:
        name = text(node.child_by_field_name("name"))
        params = node.child_by_field_name("parameters")
        body = node.child_by_field_name("body")
        in_class = scope is not None and self.out.symbols[scope].kind == "class"
        param_nodes = [p for p in params.named_children if p.type != "comment"]
        index = self._add_symbol(
            Symbol(
                kind="method" if in_class else "function",
                name=name,
                qualified_name=self._qualify(scope, name),
                signature=_header(node, body),
                start_line=line(decorators[0] if decorators else node),
                end_line=end_line(node),
                parent=scope,
                param_count=len(param_nodes),
                is_varargs=any(
                    p.type in ("list_splat_pattern", "dictionary_splat_pattern")
                    for p in param_nodes
                ),
                decorators=[_decorator_text(d) for d in decorators],
                doc=_docstring(body),
            )
        )
        # Defaults and annotations are evaluated in the enclosing scope; the body in the new one.
        # Pushed in reverse so they are visited in source order.
        stack.append((body, index, []))
        return_type = node.child_by_field_name("return_type")
        if return_type is not None:
            stack.append((return_type, scope, []))
        stack.append((params, scope, []))

    def _class(self, node: Node, scope, decorators, stack) -> None:
        name = text(node.child_by_field_name("name"))
        body = node.child_by_field_name("body")
        superclasses = node.child_by_field_name("superclasses")
        bases = []
        if superclasses is not None:
            bases = [
                one_line(text(c), MAX_SIGNATURE)
                for c in superclasses.named_children
                if c.type not in ("keyword_argument", "comment")
            ]
        index = self._add_symbol(
            Symbol(
                kind="class",
                name=name,
                qualified_name=self._qualify(scope, name),
                signature=_header(node, body),
                start_line=line(decorators[0] if decorators else node),
                end_line=end_line(node),
                parent=scope,
                decorators=[_decorator_text(d) for d in decorators],
                bases=bases,
                doc=_docstring(body),
            )
        )
        stack.append((body, index, []))
        if superclasses is not None:
            stack.append((superclasses, scope, []))

    # --- references --------------------------------------------------------------

    def _call(self, node: Node, scope) -> None:
        function = node.child_by_field_name("function")
        if function.type == "identifier":
            name_node, receiver = function, None
        elif function.type == "attribute":
            name_node = function.child_by_field_name("attribute")
            receiver = one_line(text(function.child_by_field_name("object")), MAX_RECEIVER)
        else:  # f()(), handlers[0](), (lambda: x)() -- no name to record
            return
        self.out.refs.append(
            Ref(
                kind="call",
                name=text(name_node),
                receiver=receiver,
                arg_count=_arg_count(node.child_by_field_name("arguments")),
                line=line(name_node),
                col=col(name_node),
                enclosing=scope,
            )
        )

    # --- imports -----------------------------------------------------------------

    def _import(self, node: Node) -> None:
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                module = text(child.child_by_field_name("name"))
                alias = text(child.child_by_field_name("alias"))
            else:
                module, alias = text(child), None
            self.out.imports.append(Import(module, None, alias, line(node)))

    def _import_from(self, node: Node) -> None:
        module = self._resolve_module(node.child_by_field_name("module_name"))
        if any(c.type == "wildcard_import" for c in node.named_children):
            self.out.imports.append(Import(module, "*", None, line(node)))
            return
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                name = text(child.child_by_field_name("name"))
                alias = text(child.child_by_field_name("alias"))
            else:
                name, alias = text(child), None
            self.out.imports.append(Import(module, name, alias, line(node)))

    def _resolve_module(self, node: Node) -> str:
        """'..x' in package 'a.b.c' -> 'a.b.x'. Left relative if it climbs past the top."""
        if node.type != "relative_import":
            return text(node)
        raw = text(node)
        dotted = raw.lstrip(".")
        level = len(raw) - len(dotted)
        parts = self.package.split(".") if self.package else []
        if level - 1 > len(parts):
            return raw
        base = parts[: len(parts) - (level - 1)]
        return ".".join([*base, dotted] if dotted else base) or raw


def _header(node: Node, body: Node) -> str:
    """Source text of a def/class line: 'async def f(x) -> int', without the colon."""
    source = node.text[: body.start_byte - node.start_byte].decode("utf-8", errors="replace")
    return one_line(source.rstrip().removesuffix(":"), MAX_SIGNATURE)


def _decorator_text(decorator: Node) -> str:
    return one_line(text(decorator).removeprefix("@"), MAX_SIGNATURE)


def _docstring(body: Node) -> str | None:
    first = body.named_children[0] if body.named_child_count else None
    if first is None or first.type != "expression_statement":
        return None
    string = first.named_children[0] if first.named_child_count else None
    if string is None or string.type != "string":
        return None
    raw = text(string)
    try:
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        value = raw
    if not isinstance(value, str):
        return None
    return inspect.cleandoc(value)[:MAX_DOC]


def _arg_count(arguments: Node | None) -> int | None:
    if arguments is None:
        return None
    if arguments.type == "generator_expression":  # f(x for x in y)
        return 1
    args = [a for a in arguments.named_children if a.type != "comment"]
    if any(a.type in _SPLATS for a in args):
        return None  # unknowable
    return len(args)
