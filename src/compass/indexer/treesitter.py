"""Small helpers shared by the language extractors."""

import re
from functools import cache

import tree_sitter_java
import tree_sitter_python
from tree_sitter import Language, Node, Parser

_GRAMMARS = {"java": tree_sitter_java.language, "python": tree_sitter_python.language}

MAX_SIGNATURE = 200
MAX_RECEIVER = 80
MAX_DOC = 2000

_SPACE = re.compile(r"\s+")


@cache
def parser_for(language: str) -> Parser:
    return Parser(Language(_GRAMMARS[language]()))


def text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace")


def one_line(s: str, limit: int) -> str:
    """Collapse whitespace and cap the length (with '...' when cut)."""
    s = _SPACE.sub(" ", s).strip()
    return s if len(s) <= limit else s[: limit - 3] + "..."


def line(node: Node) -> int:
    return node.start_point[0] + 1


def col(node: Node) -> int:
    return node.start_point[1] + 1


def end_line(node: Node) -> int:
    return node.end_point[0] + 1
