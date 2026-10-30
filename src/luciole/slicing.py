"""The static graph indexed for origin queries (origins.py): its edges walked backwards, and the
source lines and statements they point at."""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

CODE_WIDTH = 100  # characters of a line of code shown


class Graph:
    """The static graph, indexed for walking it backwards, and the source lines it points at."""

    def __init__(self, graph: dict):
        self.root = Path(graph.get("root") or ".")
        self.nodes = {node["id"]: node for node in graph["nodes"]}
        self.into: dict[tuple[str, str], list[dict]] = {}
        self.out: dict[tuple[str, str], list[dict]] = {}
        for edge in graph["edges"]:
            self.into.setdefault((edge["kind"], edge["target"]), []).append(edge)
            self.out.setdefault((edge["kind"], edge["source"]), []).append(edge)
        self._files: dict[str, tuple | None] = {}  # file: (lines, statements, loops)

    def resolve(self, function: str, variable: str) -> str:
        """The node id of `variable` as code in `function` names it."""
        if "#" in variable:
            return variable
        own = f"{function}#{variable}"
        if variable == "return" or own in self.nodes:
            return own
        name = variable.split(".", 1)[1] if variable.startswith(("self.", "cls.")) else variable
        if name != variable:  # an attribute: of the method's class, else of one it reads or writes
            parent = self.nodes.get(function, {}).get("parent")
            if f"{parent}#{name}" in self.nodes:
                return f"{parent}#{name}"
        touched = [edge["source"] for edge in self.into.get(("reads", function), [])] + [edge["target"] for edge in self.out.get(("writes", function), [])]
        for var in touched:
            found = self.nodes.get(var)
            if found and found["name"] == name and found.get("role") in (("instance_attr", "class_attr") if name != variable else ("global", "instance_attr", "class_attr")):
                return var
        if name == variable:
            module = function.split(":", 1)[0]
            if f"{module}#{name}" in self.nodes:
                return f"{module}#{name}"
        return own  # unknown to the graph: a local the analysis does not see, perhaps

    def role(self, var: str) -> str | None:
        found = self.nodes.get(var)
        if found:
            return found.get("role")
        return "return" if var.endswith("#return") else None

    def label(self, var: str) -> str:
        """A variable as code names it: `due`, `Cart.subtotal`, `DISCOUNT`."""
        owner, _, name = var.rpartition("#")
        if self.role(var) in ("instance_attr", "class_attr"):
            return f"{owner.rsplit(':', 1)[-1].rsplit('.', 1)[-1]}.{name}"
        return name

    def sites(self, kind: str, source: str | None, target: str) -> list[int]:
        """Lines of `kind` edges into `target` (from `source` only, when given)."""
        lines = set()
        for edge in self.into.get((kind, target), []):
            if source is None or edge["source"] == source:
                lines.update(site["line"] for site in edge.get("sites", []))
        return sorted(lines)

    def edges_at(self, kind: str, target: str, span: tuple[int, int]) -> list[dict]:
        return [edge for edge in self.into.get((kind, target), []) if any(span[0] <= site["line"] <= span[1] for site in edge.get("sites", []))]

    def owner(self, var: str) -> str:
        return var.rpartition("#")[0]

    def file_of(self, node: str) -> str | None:
        while node:
            found = self.nodes.get(node)
            if found is None:
                node = node.rsplit(".", 1)[0] if "." in node.split(":")[-1] else node.split(":")[0] if ":" in node else ""
                continue
            return found.get("file")
        return None

    def span(self, node: str, line: int | None) -> tuple[int, int]:
        """The lines of the statement at `line` in `node`'s file (a compound statement's header
        only), so that edge sites and call lines anywhere in it match."""
        if line is None:
            return (0, -1)
        source = self._source(node)
        return source[1].get(line, (line, line)) if source else (line, line)

    def code(self, node: str, line: int | None) -> str | None:
        """The statement at `line`, on one line."""
        source = self._source(node)
        if not source or line is None:
            return None
        start, end = source[1].get(line, (line, line))
        text = " ".join(part.strip() for part in source[0][start - 1 : end])
        return text if len(text) <= CODE_WIDTH else text[: CODE_WIDTH - 1] + "…"

    def statement(self, node: str, line: int) -> ast.stmt | None:
        """The statement at `line` (a compound statement's header is not one)."""
        source = self._source(node)
        if not source:
            return None
        start, end = source[1].get(line, (line, line))
        try:
            return ast.parse(textwrap.dedent("\n".join(source[0][start - 1 : end]))).body[0]
        except (SyntaxError, IndexError, ValueError):
            return None

    def iterated(self, node: str, line: int) -> list[str]:
        """The variables the comprehensions at `line` iterate over (`xs` in `[f(x) for x in xs]`),
        when the call at `line` is inside one."""
        statement = self.statement(node, line)
        if statement is None:
            return []
        names = []
        for found in ast.walk(statement):
            if isinstance(found, ast.comprehension):
                names += [name.id for name in ast.walk(found.iter) if isinstance(name, ast.Name)]
        variables = [self.resolve(node, name) for name in dict.fromkeys(names)]
        return [var for var in variables if var in self.nodes]

    def in_one_loop(self, node: str, line: int, other: int) -> bool:
        """Whether a loop holds both lines, so that `line` may run after `other`."""
        source = self._source(node)
        return bool(source) and any(start <= line <= end and start <= other <= end for start, end in source[2])

    def call_assignment(self, node: str, line: int, name: str) -> bool:
        """Whether the statement at `line` is `name = f(...)` (or `name = await f(...)`): the
        variable is then exactly what the call returned."""
        statement = self.statement(node, line)
        if statement is None:
            return False
        value = statement.value if isinstance(statement, (ast.Assign, ast.AnnAssign)) else None
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target] if isinstance(statement, ast.AnnAssign) else []
        value = value.value if isinstance(value, ast.Await) else value
        return isinstance(value, ast.Call) and len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id == name

    def _source(self, node: str):
        file = self.file_of(node)
        if file is None:
            return None
        if file not in self._files:
            try:
                text = (self.root / file).read_text(encoding="utf-8")
                tree = ast.parse(text)
                loops = [(loop.lineno, loop.end_lineno or loop.lineno) for loop in ast.walk(tree) if isinstance(loop, (ast.For, ast.AsyncFor, ast.While))]
                self._files[file] = (text.splitlines(), _statements(tree), loops)
            except (OSError, SyntaxError, ValueError):
                self._files[file] = None
        return self._files[file]


def _statements(tree: ast.AST) -> dict[int, tuple[int, int]]:
    """{line: (first, last line of the innermost statement there)}; for a compound statement
    (`for`, `if`, `with`, `def`), its header only."""
    spans: dict[int, tuple[int, int]] = {}
    for node in ast.walk(tree):  # outer statements first: inner ones overwrite them
        if not isinstance(node, ast.stmt):
            continue
        body = getattr(node, "body", None)
        end = node.end_lineno or node.lineno
        if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
            end = max(node.lineno, body[0].lineno - 1)
        for line in range(node.lineno, end + 1):
            spans[line] = (node.lineno, end)
    return spans
