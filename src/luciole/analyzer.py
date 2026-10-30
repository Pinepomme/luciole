"""Static analysis: turn Python source into a Luciole graph (see docs/graph-format.md).

Pass 1 (`Analyzer.collect*`) creates every module, class, function and variable node and
records what each name is bound to in each scope. Pass 2 (`FlowVisitor`, in flows.py) walks the
code again, resolves names against those bindings, and records the edges. The scopes and AST
helpers both passes share are in scopes.py.
"""

from __future__ import annotations

import ast
import fnmatch
import logging
import os
from pathlib import Path

from luciole import __version__
from luciole.flows import FlowVisitor
from luciole.scopes import BUILTIN_METHOD_NAMES, Binding, FUNCTION_DEFS, Import, MAX_OVERRIDES, PROPERTY_DECORATORS, Param, Scope, VARIABLE_ROLE, body_line, is_overload, is_super, loc, pos, scope_walk

log = logging.getLogger(__name__)

SKIP_DIRS = {"__pycache__", "node_modules", "site-packages", "venv"}


def build_graph(path: Path) -> dict:
    """Analyze a Python file or a directory tree and return the graph as a JSON-ready dict."""
    if path.is_file():
        root, files = path.parent, [path]
    else:
        root, files = path, sorted(python_files(path))
    analyzer = Analyzer(root)
    modules = [analyzer.collect_module(f) for f in files]
    for scope in modules:
        if scope is not None:
            FlowVisitor(analyzer, scope, scope.node_id).run(scope.tree.body)
    return analyzer.to_json()


def python_files(root: Path):
    """The .py files under `root`, in a stable order. Hidden directories, `SKIP_DIRS` and the
    directories the top-level .gitignore lists (see `gitignored`) are left out, unvisited."""
    ignored = gitignored(root)
    for directory, subdirectories, files in os.walk(root):
        top = Path(directory) == root
        subdirectories[:] = sorted(d for d in subdirectories if not d.startswith(".") and d not in SKIP_DIRS and not ignored(d, top))
        for name in sorted(files):
            if name.endswith(".py"):
                yield Path(directory, name)


def gitignored(root: Path):
    """A test for the directories `root`'s .gitignore lists, called with a directory's name and
    whether it is at the top. Only plain patterns are used (`runs/`, `/data`, `output`, `*.egg-info`);
    paths and `**` are not, and a `!` pattern, which may bring files back, disables it all."""
    anywhere, at_top = [], []
    try:
        lines = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        lines = []
    for line in lines:
        line = line.strip()
        if line.startswith("!"):
            return lambda name, top: False
        pattern = line.strip("/")
        if not pattern or line.startswith("#") or "/" in pattern or "**" in pattern or "\\" in pattern:
            continue
        (at_top if line.startswith("/") else anywhere).append(pattern)
    return lambda name, top: any(fnmatch.fnmatchcase(name, p) for p in anywhere) or (top and any(fnmatch.fnmatchcase(name, p) for p in at_top))


def module_name(file: Path) -> str:
    """Dotted name the file is imported as: walk up through directories that are packages."""
    file = file.resolve()
    parts = [] if file.stem == "__init__" else [file.stem]
    directory = file.parent
    while (directory / "__init__.py").exists() and directory.parent != directory:
        parts.insert(0, directory.name)
        directory = directory.parent
    return ".".join(parts)


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
        self.subclasses: dict[str, list[str]] | None = None  # class id -> direct subclasses, built on first use
        self.return_types: dict[str, str | None] = {}  # function id -> class of what it returns
        self.aliases: dict[str, str] = {}  # variable id -> function it holds (`parse = staticmethod(parse_header)`)
        self.method_names: dict[str, list[str]] | None = None  # method name -> functions defined in classes
        self.by_name: set[tuple[str, str, str]] = set()  # edges resolved by method name alone
        self.callbacks: set[tuple[str, str, str]] = set()  # functions passed to be called later
        self.module_bindings: dict[tuple[str, str], str | None] = {}  # (module id, name) -> resolved, see module_binding

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

    def add_edge(self, kind: str, source: str, target: str, site: tuple[int, int], by_name: bool = False, callback: bool = False) -> None:
        """Record an edge. `by_name`: a call resolved by method name alone; `callback`: a function
        passed to be called later. Finding the same edge as a plain call makes it a plain call."""
        key = (kind, source, target)
        if not by_name and not callback:
            self.by_name.discard(key)
            self.callbacks.discard(key)
        elif key not in self.edges:
            (self.by_name if by_name else self.callbacks).add(key)
        sites = self.edges.setdefault(key, [])
        if site not in sites:
            sites.append(site)

    def only_method_named(self, name: str) -> str | None:
        """The project's one method called `name`, when there is exactly one and no built-in type
        has a method of that name: the likely target of `x.name` when `x`'s type is unknown."""
        if self.method_names is None:
            self.method_names = {}
            for scope in self.scopes.values():
                if scope.kind == "function" and scope.parent is not None and scope.parent.kind == "class":
                    self.method_names.setdefault(self.nodes[scope.node_id]["name"], []).append(scope.node_id)
        found = self.method_names.get(name, [])
        return found[0] if len(found) == 1 and name not in BUILTIN_METHOD_NAMES else None

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
                {
                    "kind": kind,
                    "source": source,
                    "target": target,
                    "sites": [{"line": l, "col": c} for l, c in sorted(sites)],
                    **({"by_name": True} if (kind, source, target) in self.by_name else {}),
                    **({"callback": True} if (kind, source, target) in self.callbacks else {}),
                }
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
                    elif base is not None and scope.kind == "module":
                        scope.star_imports.append(base)

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
        accessor = any(isinstance(d, ast.Attribute) and d.attr in ("setter", "deleter") and isinstance(d.value, ast.Name) and d.value.id == fn.name for d in fn.decorator_list)
        if not (accessor and fn.name in scope.bindings):  # `obj.x` reads the property's getter
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

    def resolve(self, binding: Binding, seen: set[tuple[str, str]] | None = None) -> str | None:
        if not isinstance(binding, Import):
            return binding
        if binding.name is None:
            module = self.modules.get(binding.module)
            return module.node_id if module else None
        module = self.modules.get(binding.module)
        found = self.module_binding(module, binding.name, seen) if module else None
        if found:
            return found
        submodule = self.modules.get(f"{binding.module}.{binding.name}" if binding.module else binding.name)
        return submodule.node_id if submodule else None

    def module_binding(self, module: Scope, name: str, seen: set[tuple[str, str]] | None = None) -> str | None:
        """A module's own binding for `name`, else one its `from m import *` lines bring in.

        `seen` holds the (module, name) pairs already being looked up, so import cycles end; the
        answers of outermost lookups are cached, which keeps star imports between many modules linear.
        """
        key = (module.node_id, name)
        outermost = seen is None
        if outermost:
            if key in self.module_bindings:
                return self.module_bindings[key]
            seen = set()
        elif key in seen:  # import cycle
            return None
        seen.add(key)
        found = None
        if name in module.bindings:
            found = self.resolve(module.bindings[name], seen)
        elif not name.startswith("_"):  # star imports leave out _private names
            for star in module.star_imports:
                source = self.modules.get(star)
                found = self.module_binding(source, name, seen) if source else None
                if found:
                    break
        if outermost:
            self.module_bindings[key] = found
        return found

    def binding_scope(self, scope: Scope, name: str, shadowed: list[set[str]] = ()) -> Scope | None:
        """The scope whose binding a name read in `scope` sees (LEGB; class bodies are not visible
        from methods); the module when only its star imports may bind it."""
        if any(name in names for names in shadowed):
            return None
        current, first = scope, True
        while current is not None:
            if first or current.kind != "class":
                if name in current.global_names:
                    return self.module_scope(current)
                if (name in current.bindings and name not in current.nonlocal_names) or current.parent is None:
                    return current
            current, first = current.parent, False
        return None

    def lookup(self, scope: Scope, name: str, shadowed: list[set[str]] = ()) -> str | None:
        """Resolve a name read in `scope`."""
        found = self.binding_scope(scope, name, shadowed)
        if found is None:
            return None
        return self.module_binding(found, name) if found.kind == "module" else self.resolve(found.bindings[name])

    def is_imported(self, scope: Scope, name: str, shadowed: list[set[str]] = ()) -> bool:
        """A name read in `scope` is bound by an import (resolved or not)."""
        found = self.binding_scope(scope, name, shadowed)
        binding = found.bindings.get(name, False) if found else False
        return binding is None or isinstance(binding, Import)

    def ref(self, scope: Scope, node: ast.expr, shadowed: list[set[str]] = ()) -> str | None:
        """Resolve a name or dotted attribute chain to a node id, without recording edges."""
        if isinstance(node, ast.Name):
            return self.lookup(scope, node.id, shadowed)
        if isinstance(node, ast.Attribute):
            if is_super(node.value):
                cls = self.enclosing_class(scope)
                found = (self.class_member(base, node.attr) for base in self.class_bases(cls)) if cls else ()
                return next(filter(None, found), None)
            base = self.ref(scope, node.value, shadowed)
            return self.member(base, node.attr) if base else None
        return None

    def member(self, node_id: str, name: str) -> str | None:
        kind = self.kind(node_id)
        if kind == "module":
            found = self.module_binding(self.scopes[node_id], name)
            if found:
                return found
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

    def is_property(self, node_id: str | None) -> bool:
        decorators = self.nodes[node_id].get("decorators", []) if self.kind(node_id) == "function" else []
        return any(d.rpartition(".")[2] in PROPERTY_DECORATORS for d in decorators)

    def enclosing_class(self, scope: Scope) -> str | None:
        """The class of the method `scope` is (or is nested in), for `super()`."""
        while scope is not None:
            if scope.kind == "function" and scope.parent is not None and scope.parent.kind == "class":
                return scope.parent.node_id
            scope = scope.parent
        return None

    def overrides(self, class_id: str, name: str) -> list[str]:
        """Functions named `name` defined in the subclasses of `class_id`, at any depth: what a
        call on an instance may run instead, when the instance is of a subclass."""
        if self.subclasses is None:
            self.subclasses = {}
            for cid, scope in self.scopes.items():
                if scope.kind == "class":
                    for base in self.class_bases(cid):
                        self.subclasses.setdefault(base, []).append(cid)
        found, stack, seen = [], list(self.subclasses.get(class_id, [])), {class_id}
        while stack and len(found) < MAX_OVERRIDES:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            own = self.resolve(self.scopes[cid].bindings.get(name))
            if self.kind(own) == "function":
                found.append(own)
            stack.extend(self.subclasses.get(cid, []))
        return found

    def return_type(self, function_id: str, depth: int = 0) -> str | None:
        """Class of the instances a function returns: from its return annotation, else from its
        `return` statements (`return Cart(...)`, `return self`, a call to a function of known type)."""
        if function_id in self.return_types:
            return self.return_types[function_id]
        self.return_types[function_id] = None  # recursion guard
        scope = self.scopes[function_id]
        found = self.annotation_class(scope.parent, scope.tree.returns) if scope.tree.returns else None
        if found is None and depth < 10:
            for node in scope_walk(scope.tree.body):
                if isinstance(node, ast.Return) and node.value is not None:
                    found = self.value_class(scope, node.value, depth + 1)
                    if found:
                        break
        self.return_types[function_id] = found
        return found

    def annotation_class(self, scope: Scope, annotation: ast.expr) -> str | None:
        """The class an annotation names: `Cart`, `"Cart"`, `Cart | None`, `Optional[Cart]`."""
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return self.annotation_class(scope, annotation.left) or self.annotation_class(scope, annotation.right)
        if isinstance(annotation, ast.Subscript) and ast.unparse(annotation.value).rpartition(".")[2] == "Optional":
            return self.annotation_class(scope, annotation.slice)
        found = self.ref(scope, annotation)
        return found if self.kind(found) == "class" else None

    def value_class(self, scope: Scope, node: ast.expr, depth: int = 0) -> str | None:
        """Class of the instance an expression evaluates to, when the code makes it plain."""
        if isinstance(node, ast.Call):
            callee = self.ref(scope, node.func)
            if self.kind(callee) == "class":
                return callee
            if self.kind(callee) == "function":
                return self.return_type(callee, depth)
            if isinstance(node.func, ast.Attribute):
                receiver = self.value_class(scope, node.func.value, depth)
                method = self.class_member(receiver, node.func.attr) if receiver else None
                return self.return_type(method, depth) if self.kind(method) == "function" else None
            return None
        if isinstance(node, (ast.Name, ast.Attribute)):
            found = self.ref(scope, node)
            if self.is_variable(found):
                return self.var_types.get(found)
            if self.is_property(found):
                return self.return_type(found, depth)
        return None

    def class_bases(self, class_id: str) -> list[str]:
        if class_id not in self.bases:
            scope = self.scopes[class_id]
            refs = [self.ref(scope.parent, b) for b in scope.tree.bases]
            self.bases[class_id] = [r for r in refs if self.kind(r) == "class"]
        return self.bases[class_id]
