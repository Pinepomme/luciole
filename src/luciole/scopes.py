"""What the analyzer's two passes share: scopes and their bindings, and small AST helpers."""

from __future__ import annotations

import ast
import io
from dataclasses import dataclass, field

FUNCTION_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
VARIABLE_ROLE = {"module": "global", "class": "class_attr", "function": "local"}
PROPERTY_DECORATORS = {"property", "cached_property", "abstractproperty"}
MAX_OVERRIDES = 30  # override edges per call, so a call on a base of a big hierarchy stays readable
# Operators and builtins that call a special method of their operand.
BINARY_DUNDERS = {
    ast.Add: "__add__", ast.Sub: "__sub__", ast.Mult: "__mul__", ast.MatMult: "__matmul__", ast.Div: "__truediv__",
    ast.FloorDiv: "__floordiv__", ast.Mod: "__mod__", ast.Pow: "__pow__", ast.BitOr: "__or__", ast.BitAnd: "__and__",
    ast.BitXor: "__xor__", ast.LShift: "__lshift__", ast.RShift: "__rshift__",
}
COMPARE_DUNDERS = {ast.Eq: "__eq__", ast.NotEq: "__ne__", ast.Lt: "__lt__", ast.LtE: "__le__", ast.Gt: "__gt__", ast.GtE: "__ge__"}
# Method names of built-in types: `x.split()` on an unknown `x` is a str's far more often than the
# project's only `split`, so these names are never matched by name alone.
BUILTIN_METHOD_NAMES = {
    name for kind in (object, str, bytes, bytearray, int, float, list, tuple, dict, set, frozenset, io.TextIOWrapper, io.BytesIO) for name in dir(kind)
}
BUILTIN_DUNDERS = {"len": "__len__", "str": "__str__", "repr": "__repr__", "iter": "__iter__", "next": "__next__", "bool": "__bool__", "hash": "__hash__", "format": "__format__", "abs": "__abs__"}
CONVERSION_DUNDERS = {ord("r"): "__repr__", ord("a"): "__repr__", ord("s"): "__str__"}  # f"{x!r}", ascii() calls repr()


@dataclass(frozen=True)
class Import:
    """A name bound by an import, resolved lazily once every module is known."""

    module: str
    name: str | None = None


# What a name is bound to in a scope: a node id, an import, or None for a name that is
# bound but not tracked (an import from outside the project, for instance).
Binding = str | Import | None


@dataclass
class Param:
    name: str
    var_id: str
    kind: str  # "posonly", "positional", "vararg", "kwonly" or "kwarg"
    node: ast.arg


@dataclass
class Scope:
    node_id: str
    kind: str  # "module", "class" or "function"
    tree: ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
    parent: Scope | None
    file: str
    bindings: dict[str, Binding] = field(default_factory=dict)
    global_names: set[str] = field(default_factory=set)
    nonlocal_names: set[str] = field(default_factory=set)
    children: list[Scope] = field(default_factory=list)
    # Modules only: the package that relative imports start from, and `from m import *` modules.
    package: str = ""
    star_imports: list[str] = field(default_factory=list)
    # Classes only: `self.<name>` attributes assigned in methods.
    attrs: dict[str, str] = field(default_factory=dict)
    # Functions only.
    params: list[Param] = field(default_factory=list)
    return_var: str | None = None
    method_kind: str | None = None  # "method", "static" or "class"; None outside classes


def scope_walk(nodes: list[ast.AST]):
    """Yield the nodes evaluated directly in one scope, in source order.

    Nested function and class definitions are yielded but not entered. Lambdas and
    comprehensions are skipped: their variables are not tracked.
    """
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Lambda, *COMPREHENSIONS)):
            continue
        yield node
        if not isinstance(node, (*FUNCTION_DEFS, ast.ClassDef)):
            stack.extend(reversed(list(ast.iter_child_nodes(node))))


def is_super(node: ast.expr) -> bool:
    """`super()` or `super(Class, self)`."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super"


def is_type_checking(test: ast.expr) -> bool:
    """`TYPE_CHECKING` or `typing.TYPE_CHECKING`: true for type checkers only."""
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def is_overload(node: ast.AST) -> bool:
    """`@typing.overload` stubs only exist for type checkers; they are left out of the graph."""
    return isinstance(node, FUNCTION_DEFS) and any(
        (isinstance(d, ast.Name) and d.id == "overload") or (isinstance(d, ast.Attribute) and d.attr == "overload")
        for d in node.decorator_list
    )


def loc(node: ast.AST) -> dict:
    return {"line": node.lineno, "col": node.col_offset, "end_line": node.end_lineno, "end_col": node.end_col_offset}


def body_line(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """First line that runs when the function is called: its first statement after the docstring.

    A breakpoint there stops on each call; one on the `def` line stops when the function is defined.
    """
    body = fn.body
    has_docstring = isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)
    return body[1].lineno if has_docstring and len(body) > 1 else body[0].lineno


def pos(node: ast.AST) -> tuple[int, int]:
    return (node.lineno, node.col_offset)
