"""Pass 2 of the analyzer: walk each scope's code and record the graph's edges."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from luciole.scopes import BINARY_DUNDERS, BUILTIN_DUNDERS, COMPARE_DUNDERS, COMPREHENSIONS, CONVERSION_DUNDERS, FUNCTION_DEFS, Param, Scope, is_overload, is_super, is_type_checking, pos

if TYPE_CHECKING:
    from luciole.analyzer import Analyzer


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
        self.type_checking = False  # in an `if TYPE_CHECKING:` block

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
            cls = self.an.annotation_class(self.scope, node.annotation)
            var = self.ref(node.target)
            if cls and self.an.is_variable(var):
                self.an.var_types[var] = cls
            if node.value:  # the value's class, when plain, is more precise than the annotation
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
            self.dunder(node.iter, "__aiter__" if isinstance(node, ast.AsyncFor) else "__iter__", node.iter)
            self.assign(self.targets(node.target), self.expr(node.iter))
            self.run(node.body)
            self.run(node.orelse)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            is_async = isinstance(node, ast.AsyncWith)
            for item in node.items:
                flows = self.expr(item.context_expr)
                self.dunder(item.context_expr, "__aenter__" if is_async else "__enter__", item.context_expr)
                self.dunder(item.context_expr, "__aexit__" if is_async else "__exit__", item.context_expr)
                if item.optional_vars:
                    targets = self.targets(item.optional_vars)
                    self.assign(targets, flows)
                    # `as x` is what __enter__ returns, often the context manager itself.
                    cls = self.an.value_class(self.scope, item.context_expr)
                    enter = self.an.class_member(cls, "__aenter__" if is_async else "__enter__") if cls else None
                    entered = self.an.return_type(enter) if self.an.kind(enter) == "function" else None
                    for var, target in targets:
                        if entered and target is item.optional_vars:
                            self.an.var_types[var] = entered
            self.run(node.body)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            self.imports(node)
        elif isinstance(node, ast.If) and is_type_checking(node.test):
            # Imports for type checkers only: they never run, but their names still resolve.
            self.expr(node.test)
            outer, self.type_checking = self.type_checking, True
            self.run(node.body)
            self.type_checking = outer
            self.run(node.orelse)
        elif not isinstance(node, (ast.Global, ast.Nonlocal)):
            self.generic(node)

    def imports(self, node: ast.Import | ast.ImportFrom) -> None:
        """Importing a module runs its code, and its packages' first: calls to each module."""
        if self.type_checking:
            return
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            base = self.an.import_base(self.scope, node)
            if base is None:
                return
            names = [base] + [f"{base}.{alias.name}" if base else alias.name for alias in node.names if alias.name != "*"]
        own = self.an.module_scope(self.scope).node_id
        for name in names:
            parts = name.split(".")
            for end in range(1, len(parts) + 1):
                module = self.an.modules.get(".".join(parts[:end]))
                if module is not None and module.node_id != own:
                    self.an.add_edge("calls", self.actor, module.node_id, pos(node))

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
        cls = self.an.value_class(self.scope, value)
        held = self.held_function(value)
        for var, node in targets:
            if node is target:
                if cls:  # the latest assignment wins: code mostly runs in source order
                    self.an.var_types[var] = cls
                if held:
                    self.an.aliases.setdefault(var, held)

    def held_function(self, value: ast.expr) -> str | None:
        """The function a value is, when a variable is set to one: `f`, `staticmethod(f)`."""
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("staticmethod", "classmethod") and len(value.args) == 1:
            value = value.args[0]
        found = self.ref(value) if isinstance(value, (ast.Name, ast.Attribute)) else None
        return found if self.an.kind(found) == "function" else None

    def dunder(self, receiver: ast.expr, name: str, site: ast.AST) -> None:
        """A special method that an operation on `receiver` calls (`x[i]`, `with x:`, `len(x)`...)."""
        cls = self.an.value_class(self.scope, receiver)
        method = self.an.class_member(cls, name) if cls else None
        if self.an.kind(method) == "function":
            self.an.add_edge("calls", self.actor, method, pos(site))

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
            if isinstance(decorator, ast.Attribute) and decorator.attr in ("setter", "getter", "deleter") and self.an.is_property(self.ref(decorator.value)):
                continue  # `@x.setter` uses the property object: no get
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
            self.dunder(node.value, "__setitem__", node)
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
            by_name = False
            if isinstance(node, ast.Attribute) and target is None:
                target = self.typed_member(node)
                if target is None and self.an.value_class(self.scope, node.value) is None and not self.from_module(node.value):
                    target = self.an.only_method_named(node.attr)
                    by_name = target is not None
            on_class = isinstance(node, ast.Attribute) and self.an.kind(self.ref(node.value)) == "class"  # `Class.prop` is the property
            if self.an.is_property(target) and not on_class:  # reading `obj.prop` runs its getter, maybe a subclass's
                self.an.add_edge("calls", self.actor, target, pos(node), by_name)
                cls = self.an.value_class(self.scope, node.value) if isinstance(node, ast.Attribute) else None
                for getter in self.an.overrides(cls, node.attr) if cls else []:
                    if self.an.is_property(getter):
                        self.an.add_edge("calls", self.actor, getter, pos(node))
                return_var = self.an.scopes[target].return_var
                return [(return_var, pos(node))] if return_var else []
            if (target and not by_name) or isinstance(node, ast.Name):
                return []
            return self.expr(node.value)  # a method guessed by name doesn't hide where `obj` comes from
        if isinstance(node, ast.Subscript):
            self.dunder(node.value, "__delitem__" if isinstance(node.ctx, ast.Del) else "__getitem__", node)
        elif isinstance(node, ast.Compare):
            for left, op, right in zip([node.left, *node.comparators], node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    self.dunder(right, "__contains__", node)
                elif type(op) in COMPARE_DUNDERS:
                    self.dunder(left, COMPARE_DUNDERS[type(op)], node)
        elif isinstance(node, ast.BinOp) and type(node.op) in BINARY_DUNDERS:
            self.dunder(node.left, BINARY_DUNDERS[type(node.op)], node)
        elif isinstance(node, ast.FormattedValue) and node.conversion in CONVERSION_DUNDERS:  # f"{x!r}"
            self.dunder(node.value, CONVERSION_DUNDERS[node.conversion], node)
        elif isinstance(node, ast.FormattedValue):
            cls = self.an.value_class(self.scope, node.value)
            self.dunder(node.value, "__format__" if cls and self.an.class_member(cls, "__format__") else "__str__", node)
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

    def typed_member(self, node: ast.Attribute) -> str | None:
        """`x.m` when `x` itself has no node but its class is plain: `make().m`, `self.items[0].m`."""
        cls = self.an.value_class(self.scope, node.value)
        return self.an.class_member(cls, node.attr) if cls else None

    def from_module(self, node: ast.expr) -> bool:
        """A module, or attributes on one that the project doesn't define (`os.path`, `json` or a
        name imported from outside the project): no instance of a project class, so `node.m` is
        never matched by method name."""
        while isinstance(node, ast.Attribute):
            found = self.ref(node)
            if found is not None:
                return self.an.kind(found) == "module"
            node = node.value
        if not isinstance(node, ast.Name):
            return False
        found = self.lookup(node.id)
        if found is not None:
            return self.an.kind(found) == "module"
        return self.an.is_imported(self.scope, node.id, self.shadowed)

    def instance_class(self, func: ast.expr) -> str | None:
        """`type(x)` or `x.__class__`, called to make another instance of `x`'s class."""
        if isinstance(func, ast.Attribute) and func.attr == "__class__":
            return self.an.value_class(self.scope, func.value)
        if isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "type" and len(func.args) == 1:
            return self.an.value_class(self.scope, func.args[0])
        return None

    def call(self, node: ast.Call) -> list[Flow]:
        site = pos(node)
        self.callback_args(node)
        target = self.ref(node.func)
        if target is None and isinstance(node.func, ast.Attribute):
            target = self.typed_member(node.func)
        target = target or self.instance_class(node.func)
        by_name = False
        if target is None and isinstance(node.func, ast.Attribute) and self.an.value_class(self.scope, node.func.value) is None and not self.from_module(node.func.value):
            target = self.an.only_method_named(node.func.attr)
            by_name = target is not None
        if target is None and isinstance(node.func, ast.Name) and node.func.id in BUILTIN_DUNDERS and node.args:
            self.dunder(node.args[0], BUILTIN_DUNDERS[node.func.id], node)
        kind = self.an.kind(target)
        if kind == "variable" and target in self.an.aliases:
            target, kind = self.an.aliases[target], "function"
        elif kind == "variable" and target in self.an.var_types:
            self.dunder(node.func, "__call__", node)  # calling an instance
        self.override_calls(node.func, site)
        offset, receiver = 0, None
        if kind == "class":
            init = self.an.class_member(target, "__init__")
            if self.an.kind(init) != "function":
                self.an.add_edge("calls", self.actor, target, site)
                self.unmapped_args(node)
                return []
            target, kind, offset = init, "function", 1
        elif kind == "function" and by_name:
            # Through an instance of a class this code doesn't know: a method bound to `node.func.value`.
            is_method = self.an.scopes[target].method_kind == "method"
            offset, receiver = (1, node.func.value) if is_method else (int(self.an.scopes[target].method_kind == "class"), None)
        elif kind == "function":
            offset, receiver = self.binding(node.func, self.an.scopes[target])

        if kind != "function":
            # Unresolved call: assume anything that goes in can come out.
            flows = self.expr(node.func) if kind in (None, "variable") else []
            return flows + self.unmapped_args(node)

        self.an.add_edge("calls", self.actor, target, site, by_name)
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

    def callback_args(self, node: ast.Call) -> None:
        """A project function passed, not called, as an argument (`Thread(target=crunch)`,
        `pool.submit(work)`, `sorted(key=self.rank)`) is called later by what receives it: a
        `calls` edge marked `callback`."""
        for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
            if not isinstance(arg, (ast.Name, ast.Attribute)):
                continue
            target = self.ref(arg)
            if target is None and isinstance(arg, ast.Attribute):
                target = self.typed_member(arg)
            if self.an.kind(target) == "variable":
                target = self.an.aliases.get(target)
            if self.an.kind(target) == "function" and not self.an.is_property(target):
                self.an.add_edge("calls", self.actor, target, pos(arg), callback=True)

    def binding(self, func: ast.expr, callee: Scope) -> tuple[int, ast.expr | None]:
        """How a call binds the callee's first parameter: (argument offset, receiver expression)."""
        if callee.method_kind in (None, "static") or not isinstance(func, ast.Attribute):
            return 0, None
        if callee.method_kind == "class" or is_super(func.value):
            return 1, None
        via_instance = self.an.is_variable(self.ref(func.value)) or self.an.value_class(self.scope, func.value) is not None
        return (1, func.value) if via_instance else (0, None)

    def override_calls(self, func: ast.expr, site: tuple[int, int]) -> None:
        """`obj.m()` may run any subclass's own `m`: a call to each (not for `super().m()`)."""
        if not isinstance(func, ast.Attribute) or is_super(func.value):
            return
        cls = self.an.value_class(self.scope, func.value)  # an instance, or `cls` in a classmethod
        for method in self.an.overrides(cls, func.attr) if cls else []:
            self.an.add_edge("calls", self.actor, method, site)

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
