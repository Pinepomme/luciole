"""Static analysis: turn Python source into a Luciole graph (see docs/graph-format.md).

Pass 1 (`Analyzer.collect*`) creates every module, class, function and variable node and
records what each name is bound to in each scope. Pass 2 (`FlowVisitor`) walks the code
again, resolves names against those bindings, and records the edges.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path

from luciole import __version__

log = logging.getLogger(__name__)

SKIP_DIRS = {"__pycache__", "node_modules", "site-packages", "venv"}
FUNCTION_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
VARIABLE_ROLE = {"module": "global", "class": "class_attr", "function": "local"}


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
    # Modules only: the package that relative imports start from.
    package: str = ""
    # Classes only: `self.<name>` attributes assigned in methods.
    attrs: dict[str, str] = field(default_factory=dict)
    # Functions only.
    params: list[Param] = field(default_factory=list)
    return_var: str | None = None
    method_kind: str | None = None  # "method", "static" or "class"; None outside classes


def build_graph(path: Path) -> dict:
    """Analyze a Python file or a directory tree and return the graph as a JSON-ready dict."""
    if path.is_file():
        root, files = path.parent, [path]
    else:
        root = path
        files = sorted(
            p
            for p in path.rglob("*.py")
            if not any(part.startswith(".") or part in SKIP_DIRS for part in p.relative_to(root).parent.parts)
        )
    analyzer = Analyzer(root)
    modules = [analyzer.collect_module(f) for f in files]
    for scope in modules:
        if scope is not None:
            FlowVisitor(analyzer, scope, scope.node_id).run(scope.tree.body)
    return analyzer.to_json()


def module_name(file: Path) -> str:
    """Dotted name the file is imported as: walk up through directories that are packages."""
    file = file.resolve()
    parts = [] if file.stem == "__init__" else [file.stem]
    directory = file.parent
    while (directory / "__init__.py").exists() and directory.parent != directory:
        parts.insert(0, directory.name)
        directory = directory.parent
    return ".".join(parts)


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


class Analyzer:
    def __init__(self, root: Path):
        self.root = root
        self.nodes: dict[str, dict] = {}
        self.edges: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
        self.scopes: dict[str, Scope] = {}
        self.modules: dict[str, Scope] = {}
        self.def_scopes: dict[ast.AST, Scope] = {}
        self.var_types: dict[str, str] = {}  # variable id -> id of the class of its value
        self.self_vars: set[str] = set()
        self.bases: dict[str, list[str]] = {}

    # Nodes and edges

    def add_node(self, kind: str, name: str, parent: Scope | None, file: str, location: dict, base_id: str, **extra) -> str:
        node_id, n = base_id, 2
        while node_id in self.nodes:
            node_id, n = f"{base_id}~{n}", n + 1
        parent_id = parent.node_id if parent else None
        self.nodes[node_id] = {"id": node_id, "kind": kind, "name": name, "parent": parent_id, "file": file, "loc": location, **extra}
        return node_id

    def add_variable(self, scope: Scope, name: str, role: str, node: ast.AST, **extra) -> str:
        return self.add_node("variable", name, scope, scope.file, loc(node), f"{scope.node_id}#{name}", role=role, **extra)

    def add_edge(self, kind: str, source: str, target: str, site: tuple[int, int]) -> None:
        sites = self.edges.setdefault((kind, source, target), [])
        if site not in sites:
            sites.append(site)

    def is_variable(self, node_id: str | None) -> bool:
        return node_id is not None and self.nodes[node_id]["kind"] == "variable"

    def kind(self, node_id: str | None) -> str | None:
        return self.nodes[node_id]["kind"] if node_id else None

    def to_json(self) -> dict:
        return {
            "format": "luciole-graph",
            "format_version": 1,
            "root": self.root.as_posix(),
            "generator": {"name": "luciole", "version": __version__},
            "nodes": list(self.nodes.values()),
            "edges": [
                {"kind": kind, "source": source, "target": target, "sites": [{"line": l, "col": c} for l, c in sorted(sites)]}
                for (kind, source, target), sites in self.edges.items()
            ],
        }

    # Pass 1: definitions and bindings

    def collect_module(self, file: Path) -> Scope | None:
        try:
            tree = ast.parse(file.read_bytes(), filename=str(file))
        except (SyntaxError, ValueError, OSError) as error:
            log.warning("skipping %s: %s", file, error)
            return None
        name = module_name(file)
        rel = file.relative_to(self.root).as_posix()
        last = tree.body[-1] if tree.body else None
        location = {"line": 1, "col": 0, "end_line": last.end_lineno if last else 1, "end_col": last.end_col_offset if last else 0}
        module_id = self.add_node("module", name, None, rel, location, name)
        is_package = file.stem == "__init__"
        scope = Scope(module_id, "module", tree, None, rel, package=name if is_package else name.rpartition(".")[0])
        self.scopes[module_id] = scope
        self.modules.setdefault(name, scope)
        self.collect(scope, tree.body)
        return scope

    def collect(self, scope: Scope, body: list[ast.stmt]) -> None:
        nodes = list(scope_walk(body))
        for node in nodes:
            if isinstance(node, ast.Global):
                scope.global_names.update(node.names)
            elif isinstance(node, ast.Nonlocal):
                scope.nonlocal_names.update(node.names)
        for node in nodes:
            if is_overload(node):
                continue
            if isinstance(node, FUNCTION_DEFS):
                self.collect_function(scope, node)
            elif isinstance(node, ast.ClassDef):
                self.collect_class(scope, node)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                self.bind_variable(scope, node.id, node)
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                self.bind_variable(scope, node.name, node)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                self.bind_variable(scope, node.rest, node)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        scope.bindings[alias.asname] = Import(alias.name)
                    else:
                        top = alias.name.partition(".")[0]
                        scope.bindings[top] = Import(top)
            elif isinstance(node, ast.ImportFrom):
                base = self.import_base(scope, node)
                for alias in node.names:
                    if alias.name != "*":
                        scope.bindings[alias.asname or alias.name] = None if base is None else Import(base, alias.name)

    def bind_variable(self, scope: Scope, name: str, node: ast.AST) -> None:
        if name in scope.nonlocal_names:
            return
        if name in scope.global_names:
            scope = self.module_scope(scope)
        if name not in scope.bindings:
            scope.bindings[name] = self.add_variable(scope, name, VARIABLE_ROLE[scope.kind], node)

    def child_id(self, scope: Scope, name: str) -> str:
        return f"{scope.node_id}{':' if scope.kind == 'module' else '.'}{name}"

    def collect_function(self, scope: Scope, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        extra = {}
        if isinstance(fn, ast.AsyncFunctionDef):
            extra["is_async"] = True
        if fn.decorator_list:
            extra["decorators"] = [ast.unparse(d) for d in fn.decorator_list]
        fid = self.add_node("function", fn.name, scope, scope.file, loc(fn), self.child_id(scope, fn.name), body_line=body_line(fn), **extra)
        scope.bindings[fn.name] = fid
        fscope = Scope(fid, "function", fn, scope, scope.file)
        self.scopes[fid] = fscope
        self.def_scopes[fn] = fscope
        scope.children.append(fscope)

        if scope.kind == "class":
            names = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}
            fscope.method_kind = "static" if "staticmethod" in names else "class" if "classmethod" in names else "method"
        args = fn.args
        params = (
            [(a, "posonly") for a in args.posonlyargs]
            + [(a, "positional") for a in args.args]
            + ([(args.vararg, "vararg")] if args.vararg else [])
            + [(a, "kwonly") for a in args.kwonlyargs]
            + ([(args.kwarg, "kwarg")] if args.kwarg else [])
        )
        for index, (arg, kind) in enumerate(params):
            var_id = self.add_variable(fscope, arg.arg, "param", arg, param_index=index)
            fscope.bindings[arg.arg] = var_id
            fscope.params.append(Param(arg.arg, var_id, kind, arg))
        if fscope.method_kind in ("method", "class") and fscope.params:
            self_var = fscope.params[0].var_id
            self.self_vars.add(self_var)
            self.var_types[self_var] = scope.node_id

        self.collect(fscope, fn.body)
        first_return = next((n for n in scope_walk(fn.body) if isinstance(n, ast.Return) and n.value), None)
        if first_return:
            fscope.return_var = self.add_variable(fscope, "return", "return", first_return)

    def collect_class(self, scope: Scope, cls: ast.ClassDef) -> None:
        bases = [ast.unparse(b) for b in cls.bases]
        extra = {"decorators": [ast.unparse(d) for d in cls.decorator_list]} if cls.decorator_list else {}
        cid = self.add_node("class", cls.name, scope, scope.file, loc(cls), self.child_id(scope, cls.name), bases=bases, **extra)
        scope.bindings[cls.name] = cid
        cscope = Scope(cid, "class", cls, scope, scope.file)
        self.scopes[cid] = cscope
        self.def_scopes[cls] = cscope
        scope.children.append(cscope)
        self.collect(cscope, cls.body)

        # Instance attributes: `self.<name> = ...` in any regular method, first one wins.
        stores = []
        for method in cscope.children:
            if method.method_kind == "method" and method.params:
                self_name = method.params[0].name
                stores += [
                    n
                    for n in scope_walk(method.tree.body)
                    if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) and isinstance(n.value, ast.Name) and n.value.id == self_name
                ]
        for node in sorted(stores, key=pos):
            if node.attr in cscope.attrs:
                continue
            existing = cscope.bindings.get(node.attr)
            if self.is_variable(existing if isinstance(existing, str) else None):
                cscope.attrs[node.attr] = existing
            else:
                cscope.attrs[node.attr] = self.add_variable(cscope, node.attr, "instance_attr", node)

    def import_base(self, scope: Scope, node: ast.ImportFrom) -> str | None:
        """Absolute module name of a `from ... import`; "" stands for the top level."""
        if node.level == 0:
            return node.module
        parts = self.module_scope(scope).package.split(".") if self.module_scope(scope).package else []
        if node.level - 1 > len(parts):
            return None
        parts = parts[: len(parts) - (node.level - 1)]
        if node.module:
            parts.append(node.module)
        return ".".join(parts)

    # Name resolution

    def module_scope(self, scope: Scope) -> Scope:
        while scope.parent is not None:
            scope = scope.parent
        return scope

    def resolve(self, binding: Binding, depth: int = 0) -> str | None:
        if not isinstance(binding, Import):
            return binding
        if depth > 20:  # import cycle
            return None
        if binding.name is None:
            module = self.modules.get(binding.module)
            return module.node_id if module else None
        module = self.modules.get(binding.module)
        if module and binding.name in module.bindings:
            return self.resolve(module.bindings[binding.name], depth + 1)
        submodule = self.modules.get(f"{binding.module}.{binding.name}" if binding.module else binding.name)
        return submodule.node_id if submodule else None

    def lookup(self, scope: Scope, name: str, shadowed: list[set[str]] = ()) -> str | None:
        """Resolve a name read in `scope` (LEGB; class bodies are not visible from methods)."""
        if any(name in names for names in shadowed):
            return None
        current, first = scope, True
        while current is not None:
            if first or current.kind != "class":
                if name in current.global_names:
                    return self.resolve(self.module_scope(current).bindings.get(name))
                if name in current.bindings and name not in current.nonlocal_names:
                    return self.resolve(current.bindings[name])
            current, first = current.parent, False
        return None

    def ref(self, scope: Scope, node: ast.expr, shadowed: list[set[str]] = ()) -> str | None:
        """Resolve a name or dotted attribute chain to a node id, without recording edges."""
        if isinstance(node, ast.Name):
            return self.lookup(scope, node.id, shadowed)
        if isinstance(node, ast.Attribute):
            base = self.ref(scope, node.value, shadowed)
            return self.member(base, node.attr) if base else None
        return None

    def member(self, node_id: str, name: str) -> str | None:
        kind = self.kind(node_id)
        if kind == "module":
            module = self.scopes[node_id]
            if name in module.bindings:
                return self.resolve(module.bindings[name])
            submodule = self.modules.get(f"{self.nodes[node_id]['name']}.{name}")
            return submodule.node_id if submodule else None
        if kind == "class":
            return self.class_member(node_id, name)
        if kind == "variable" and node_id in self.var_types:
            return self.class_member(self.var_types[node_id], name)
        return None

    def class_member(self, class_id: str, name: str, seen: frozenset[str] = frozenset()) -> str | None:
        """Look a name up on a class, then on its bases (depth-first, which is close enough to the MRO)."""
        if class_id in seen:
            return None
        scope = self.scopes[class_id]
        found = self.resolve(scope.bindings.get(name)) or scope.attrs.get(name)
        if found:
            return found
        for base in self.class_bases(class_id):
            found = self.class_member(base, name, seen | {class_id})
            if found:
                return found
        return None

    def class_bases(self, class_id: str) -> list[str]:
        if class_id not in self.bases:
            scope = self.scopes[class_id]
            refs = [self.ref(scope.parent, b) for b in scope.tree.bases]
            self.bases[class_id] = [r for r in refs if self.kind(r) == "class"]
        return self.bases[class_id]


# A value source: the variable it comes from, and the call site when it is a function's
# return value (which becomes a `returns` edge instead of `flows_to`).
Flow = tuple[str, tuple[int, int] | None]


class FlowVisitor:
    """Pass 2: walk the code of one scope and record edges.

    `actor` is the module or function the code runs as. Class bodies run as the
    enclosing module or function.
    """

    def __init__(self, analyzer: Analyzer, scope: Scope, actor: str):
        self.an = analyzer
        self.scope = scope
        self.actor = actor
        self.shadowed: list[set[str]] = []  # names bound by enclosing lambdas and comprehensions

    def run(self, body: list[ast.stmt]) -> None:
        for stmt in body:
            self.stmt(stmt)

    def lookup(self, name: str) -> str | None:
        return self.an.lookup(self.scope, name, self.shadowed)

    def ref(self, node: ast.expr) -> str | None:
        return self.an.ref(self.scope, node, self.shadowed)

    def read(self, var: str, node: ast.AST) -> None:
        self.an.add_edge("reads", var, self.actor, pos(node))

    def assign(self, targets: list[tuple[str, ast.AST]], flows: list[Flow]) -> None:
        for var, node in targets:
            site = pos(node)
            self.an.add_edge("writes", self.actor, var, site)
            for source, call_site in flows:
                if source == var:
                    continue
                if call_site:
                    self.an.add_edge("returns", source, var, call_site)
                else:
                    self.an.add_edge("flows_to", source, var, site)

    # Statements

    def stmt(self, node: ast.stmt) -> None:
        if is_overload(node):
            return
        if isinstance(node, FUNCTION_DEFS):
            self.function_def(node)
        elif isinstance(node, ast.ClassDef):
            self.class_def(node)
        elif isinstance(node, ast.Assign):
            flows = self.expr(node.value)
            for target in node.targets:
                self.assign_typed(target, node.value, flows)
        elif isinstance(node, ast.AnnAssign):
            if node.value:
                self.assign_typed(node.target, node.value, self.expr(node.value))
        elif isinstance(node, ast.AugAssign):
            targets = self.targets(node.target)
            for var, target in targets:
                self.read(var, target)
            self.assign(targets, self.expr(node.value))
        elif isinstance(node, ast.Return):
            if node.value and self.scope.return_var:
                self.assign([(self.scope.return_var, node)], self.expr(node.value))
            elif node.value:  # `return` outside a function parses but never runs
                self.expr(node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            self.assign(self.targets(node.target), self.expr(node.iter))
            self.run(node.body)
            self.run(node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                flows = self.expr(item.context_expr)
                if item.optional_vars:
                    self.assign(self.targets(item.optional_vars), flows)
            self.run(node.body)
        elif not isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
            self.generic(node)

    def generic(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self.stmt(child)
            elif isinstance(child, ast.expr):
                self.expr(child)
            elif isinstance(child, (ast.excepthandler, ast.match_case)):
                self.generic(child)

    def assign_typed(self, target: ast.expr, value: ast.expr, flows: list[Flow]) -> None:
        targets = self.targets(target)
        self.assign(targets, flows)
        if isinstance(value, ast.Call) and self.an.kind(self.ref(value.func)) == "class":
            for var, node in targets:
                if node is target:
                    self.an.var_types[var] = self.ref(value.func)

    def function_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        fscope = self.an.def_scopes[node]
        self.decorators(node)
        params = {p.node: p for p in fscope.params}
        positional = node.args.posonlyargs + node.args.args
        defaults = list(zip(positional[len(positional) - len(node.args.defaults):], node.args.defaults))
        defaults += [(a, d) for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults) if d]
        for arg, default in defaults:
            var = params[arg].var_id
            for source, call_site in self.expr(default):
                if call_site:
                    self.an.add_edge("returns", source, var, call_site)
                else:
                    self.an.add_edge("flows_to", source, var, pos(arg))
        for param in fscope.params:
            if param.node.annotation and param.var_id not in self.an.var_types:
                cls = self.ref(param.node.annotation)
                if self.an.kind(cls) == "class":
                    self.an.var_types[param.var_id] = cls
        FlowVisitor(self.an, fscope, fscope.node_id).run(node.body)

    def class_def(self, node: ast.ClassDef) -> None:
        self.decorators(node)
        for base in node.bases:
            self.expr(base)
        for keyword in node.keywords:
            self.expr(keyword.value)
        FlowVisitor(self.an, self.an.def_scopes[node], self.actor).run(node.body)

    def decorators(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.expr(decorator)
            target = self.ref(decorator)
            if self.an.kind(target) == "function":
                self.an.add_edge("calls", self.actor, target, pos(decorator))

    def targets(self, node: ast.expr) -> list[tuple[str, ast.AST]]:
        """Variables written by an assignment target, each with the node to use as site."""
        if isinstance(node, (ast.Tuple, ast.List)):
            return [t for element in node.elts for t in self.targets(element)]
        if isinstance(node, ast.Starred):
            return self.targets(node.value)
        if isinstance(node, (ast.Name, ast.Attribute)):
            var = self.ref(node)
            if self.an.is_variable(var):
                return [(var, node)]
            return self.mutated(node.value, node) if isinstance(node, ast.Attribute) else []
        if isinstance(node, ast.Subscript):
            self.expr(node.slice)
            return self.mutated(node.value, node)
        return []

    def mutated(self, node: ast.expr, site: ast.AST) -> list[tuple[str, ast.AST]]:
        """`a.x = v` or `a[i] = v` with no tracked `x`: count it as a write to `a`."""
        if isinstance(node, ast.Subscript):
            self.expr(node.slice)
            return self.mutated(node.value, site)
        if isinstance(node, (ast.Name, ast.Attribute)):
            var = self.ref(node)
            if self.an.is_variable(var):
                return [(var, site)]
            return self.mutated(node.value, site) if isinstance(node, ast.Attribute) else []
        self.expr(node)
        return []

    # Expressions

    def expr(self, node: ast.expr) -> list[Flow]:
        """Record reads and calls in an expression, and return where its value comes from."""
        if isinstance(node, (ast.Name, ast.Attribute)):
            target = self.ref(node)
            if self.an.is_variable(target):
                self.read(target, node)
                return [(target, None)]
            if target or isinstance(node, ast.Name):
                return []
            return self.expr(node.value)
        if isinstance(node, ast.Call):
            return self.call(node)
        if isinstance(node, ast.NamedExpr):
            targets = self.targets(node.target)
            self.assign(targets, self.expr(node.value))
            return [(var, None) for var, _ in targets]
        if isinstance(node, ast.Lambda):
            for default in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                self.expr(default)
            args = node.args
            names = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
            names |= {a.arg for a in (args.vararg, args.kwarg) if a}
            self.shadowed.append(names)
            self.expr(node.body)
            self.shadowed.pop()
            return []
        if isinstance(node, COMPREHENSIONS):
            return self.comprehension(node)
        if isinstance(node, ast.IfExp):
            self.expr(node.test)
            return self.expr(node.body) + self.expr(node.orelse)
        flows = []
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                flows += self.expr(child)
        return flows

    def comprehension(self, node: ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp) -> list[Flow]:
        # The first iterable is evaluated outside the comprehension.
        flows = self.expr(node.generators[0].iter)
        self.shadowed.append({n.id for g in node.generators for n in ast.walk(g.target) if isinstance(n, ast.Name)})
        for index, generator in enumerate(node.generators):
            if index:
                flows += self.expr(generator.iter)
            for condition in generator.ifs:
                self.expr(condition)
        elements = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        for element in elements:
            flows += self.expr(element)
        self.shadowed.pop()
        return flows

    def call(self, node: ast.Call) -> list[Flow]:
        site = pos(node)
        target = self.ref(node.func)
        kind = self.an.kind(target)
        offset, receiver = 0, None
        if kind == "class":
            init = self.an.class_member(target, "__init__")
            if self.an.kind(init) != "function":
                self.an.add_edge("calls", self.actor, target, site)
                self.unmapped_args(node)
                return []
            target, kind, offset = init, "function", 1
        elif kind == "function":
            offset, receiver = self.binding(node.func, self.an.scopes[target])

        if kind != "function":
            # Unresolved call: assume anything that goes in can come out.
            flows = self.expr(node.func) if kind in (None, "variable") else []
            return flows + self.unmapped_args(node)

        self.an.add_edge("calls", self.actor, target, site)
        callee = self.an.scopes[target]
        if receiver is not None and callee.params:
            var = self.ref(receiver)
            flows = [(var, None)] if var in self.an.self_vars else self.expr(receiver)
            self.pass_value(flows, callee.params[0], receiver)

        positional = [p for p in callee.params if p.kind in ("posonly", "positional")]
        by_name = {p.name: p for p in callee.params if p.kind in ("positional", "kwonly")}
        vararg = next((p for p in callee.params if p.kind == "vararg"), None)
        kwarg = next((p for p in callee.params if p.kind == "kwarg"), None)
        starred = False
        for index, arg in enumerate(node.args):
            starred = starred or isinstance(arg, ast.Starred)
            if starred:  # positions after *args are unknown
                param = vararg
            else:
                param = positional[offset + index] if offset + index < len(positional) else vararg
            self.pass_value(self.expr(arg), param, arg)
        for keyword in node.keywords:
            param = by_name.get(keyword.arg, kwarg) if keyword.arg else kwarg
            self.pass_value(self.expr(keyword.value), param, keyword.value)
        return [(callee.return_var, site)] if callee.return_var else []

    def binding(self, func: ast.expr, callee: Scope) -> tuple[int, ast.expr | None]:
        """How a call binds the callee's first parameter: (argument offset, receiver expression)."""
        if callee.method_kind in (None, "static") or not isinstance(func, ast.Attribute):
            return 0, None
        if callee.method_kind == "class":
            return 1, None
        via_instance = self.an.is_variable(self.ref(func.value))
        return (1, func.value) if via_instance else (0, None)

    def pass_value(self, flows: list[Flow], param: Param | None, arg: ast.expr) -> None:
        if param is None:
            return
        for source, _ in flows:
            if source != param.var_id:
                self.an.add_edge("passes_arg", source, param.var_id, pos(arg))

    def unmapped_args(self, node: ast.Call) -> list[Flow]:
        flows = []
        for arg in node.args:
            flows += self.expr(arg)
        for keyword in node.keywords:
            flows += self.expr(keyword.value)
        return flows
