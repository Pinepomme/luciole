"""Static analyzer: each test builds a tiny project and checks the edges the spec calls for."""

import json
import textwrap
import time
from pathlib import Path

import pytest

from graphcheck import assert_valid
from luciole.analyzer import build_graph
from luciole.cli import main

REPO = Path(__file__).resolve().parent.parent


def analyze(tmp_path, files):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    graph = build_graph(tmp_path)
    assert_valid(graph)
    return graph


def edges(graph, kind):
    return {(e["source"], e["target"]) for e in graph["edges"] if e["kind"] == kind}


def node_ids(graph):
    return {n["id"] for n in graph["nodes"]}


def test_matches_hand_made_example():
    graph = build_graph(REPO / "examples" / "shop.py")
    expected = json.loads((REPO / "examples" / "shop.graph.json").read_text())
    graph["root"] = expected["root"]
    assert {n["id"]: n for n in graph["nodes"]} == {n["id"]: n for n in expected["nodes"]}
    key = lambda e: (e["kind"], e["source"], e["target"])
    assert {key(e): e for e in graph["edges"]} == {key(e): e for e in expected["edges"]}


def test_analyzes_own_source():
    graph = build_graph(REPO / "src")
    assert_valid(graph)
    assert "luciole.analyzer:FlowVisitor.call" in node_ids(graph)
    assert ("luciole.cli:main", "luciole.analyzer:build_graph") in edges(graph, "calls")


def test_imports_across_modules(tmp_path):
    graph = analyze(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/util.py": """
            def helper(x):
                return x
        """,
        "pkg/main.py": """
            from .util import helper
            from pkg import util

            def run(v):
                a = helper(v)
                b = util.helper(a)
                return b
        """,
    })
    assert ("pkg.main:run", "pkg.util:helper") in edges(graph, "calls")
    assert {("pkg.main:run#v", "pkg.util:helper#x"), ("pkg.main:run#a", "pkg.util:helper#x")} <= edges(graph, "passes_arg")
    assert ("pkg.util:helper#return", "pkg.main:run#b") in edges(graph, "returns")


def test_methods_resolve_through_base_classes(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        class Base:
            def __init__(self):
                self.n = 0

            def bump(self):
                self.n += 1

        class Child(Base):
            def go(self):
                self.bump()
                return self.n

        def main():
            c = Child()
            c.go()
    """})
    calls = edges(graph, "calls")
    assert {("m:Child.go", "m:Base.bump"), ("m:main", "m:Base.__init__"), ("m:main", "m:Child.go")} <= calls
    assert ("m:Child.go#self", "m:Base.bump#self") in edges(graph, "passes_arg")
    assert ("m:Base#n", "m:Child.go") in edges(graph, "reads")
    assert ("m:Base#n", "m:Child.go#return") in edges(graph, "flows_to")
    # `self.<name>` never counts as reading `self`.
    assert not {s for s, _ in edges(graph, "reads")} & {"m:Child.go#self", "m:Base.bump#self"}


def test_static_and_class_methods_and_keywords(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        class K:
            @staticmethod
            def s(a, b):
                pass

            @classmethod
            def c(cls, a, *rest, **opts):
                pass

        def f(x, y, z):
            K.s(x, b=y)
            K.c(x, y, flag=z)
    """})
    assert edges(graph, "passes_arg") == {
        ("m:f#x", "m:K.s#a"), ("m:f#y", "m:K.s#b"),
        ("m:f#x", "m:K.c#a"), ("m:f#y", "m:K.c#rest"), ("m:f#z", "m:K.c#opts"),
    }


def test_global_nonlocal_and_closures(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        count = 0

        def inc():
            global count
            count += 1

        def outer():
            v = 1
            def inner():
                nonlocal v
                v = 2
                return v
            return inner
    """})
    assert ("m:inc", "m#count") in edges(graph, "writes")
    assert ("m#count", "m:inc") in edges(graph, "reads")
    assert ("m:outer.inner", "m:outer#v") in edges(graph, "writes")
    assert ("m:outer#v", "m:outer.inner#return") in edges(graph, "flows_to")
    assert "m:inc#count" not in node_ids(graph)
    assert "m:outer.inner#v" not in node_ids(graph)


def test_comprehension_and_lambda_variables_are_not_tracked(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        def f(xs):
            x = 0
            ys = [x * 2 for x in xs]
            g = lambda x: x + 1
            return ys
    """})
    assert ("m:f#xs", "m:f#ys") in edges(graph, "flows_to")
    assert ("m:f#x", "m:f#ys") not in edges(graph, "flows_to")
    reads_of_x = [e for e in graph["edges"] if e["kind"] == "reads" and e["source"] == "m:f#x"]
    assert reads_of_x == []


def test_unresolved_calls_pass_values_through(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        def f(s, n):
            t = s.upper()
            u = max(n, len(t))
            return u
    """})
    assert {("m:f#s", "m:f#t"), ("m:f#n", "m:f#u"), ("m:f#t", "m:f#u")} <= edges(graph, "flows_to")
    assert edges(graph, "calls") == set()


def test_unpacking_loops_and_mutation(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        def f(pairs, table):
            for a, b in pairs:
                table[a] = b
            first, *rest = pairs
            return rest
    """})
    flows = edges(graph, "flows_to")
    assert {("m:f#pairs", "m:f#a"), ("m:f#pairs", "m:f#b"), ("m:f#b", "m:f#table")} <= flows
    assert {("m:f#pairs", "m:f#first"), ("m:f#pairs", "m:f#rest")} <= flows
    assert ("m:f#a", "m:f") in edges(graph, "reads")  # the subscript index


def test_redefinitions_get_numbered_ids(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        class C:
            @property
            def value(self):
                return 1

            @value.setter
            def value(self, v):
                pass
    """})
    assert {"m:C.value", "m:C.value~2"} <= node_ids(graph)
    setter = next(n for n in graph["nodes"] if n["id"] == "m:C.value~2")
    assert setter["decorators"] == ["value.setter"]


def test_overload_stubs_are_skipped(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        import typing
        from typing import overload

        @overload
        def f(x: int) -> int: ...
        @typing.overload
        def f(x: str) -> str: ...
        def f(x):
            return x
    """})
    functions = [n for n in graph["nodes"] if n["kind"] == "function"]
    assert [(n["id"], n["loc"]["line"]) for n in functions] == [("m:f", 9)]


def test_syntax_errors_are_skipped(tmp_path, caplog):
    graph = analyze(tmp_path, {"good.py": "x = 1\n", "bad.py": "def (:\n"})
    assert {n["id"] for n in graph["nodes"] if n["kind"] == "module"} == {"good"}
    assert "skipping" in caplog.text


def test_cli_prints_json(capsys):
    assert main(["graph", str(REPO / "examples" / "shop.py"), "--json"]) == 0
    graph = json.loads(capsys.readouterr().out)
    assert_valid(graph)


def test_cli_rejects_missing_path(tmp_path):
    with pytest.raises(SystemExit):
        main(["graph", str(tmp_path / "nope.py")])


def test_body_line_is_where_each_call_starts(tmp_path):
    graph = analyze(tmp_path, {"app.py": '''
        def plain(x):
            return x


        @decorated
        def documented(x):
            """Docstring."""
            y = x + 1
            return y


        def only_docstring():
            """Nothing else."""
    '''})
    lines = {n["name"]: n["body_line"] for n in graph["nodes"] if n["kind"] == "function"}
    assert lines == {"plain": 3, "documented": 9, "only_docstring": 14}


def calls(graph):
    return edges(graph, "calls")


def test_star_imports_and_imports_running_modules(tmp_path):
    graph = analyze(tmp_path, {
        "pkg/__init__.py": "from .queues import *\n",
        "pkg/queues.py": "class Queue:\n    def get(self):\n        return 1\n\ndef _hidden():\n    pass\n",
        "app.py": """
            import pkg
            from pkg import queues

            def main():
                q = pkg.Queue()
                return q.get()
        """,
    })
    found = calls(graph)
    assert ("app:main", "pkg.queues:Queue.get") in found  # pkg.Queue comes from the star import
    assert {("app", "pkg"), ("app", "pkg.queues"), ("pkg", "pkg.queues")} <= found  # imports run modules


def test_properties_super_and_overrides(tmp_path):
    graph = analyze(tmp_path, {"shapes.py": """
        class Shape:
            def __init__(self, name):
                self.name = name

            @property
            def area(self):
                return 0

            @area.setter
            def area(self, value):
                pass

            def describe(self):
                return self.render() + str(self.area)


        class Square(Shape):
            def __init__(self, side):
                super().__init__("square")
                self.side = side

            def render(self):
                return "[]"

            @property
            def area(self):
                return self.side ** 2


        def report(shape: Shape):
            return shape.area
    """})
    found = calls(graph)
    assert ("shapes:Square.__init__", "shapes:Shape.__init__") in found  # super()
    assert ("shapes:Shape.describe", "shapes:Square.render") in found  # defined in a subclass only
    assert ("shapes:Shape.describe", "shapes:Shape.area") in found  # the getter, not the setter
    assert {("shapes:report", "shapes:Shape.area"), ("shapes:report", "shapes:Square.area")} <= found  # and overrides


def test_types_from_returns_annotations_and_special_methods(tmp_path):
    graph = analyze(tmp_path, {"store.py": """
        class Cart:
            def __getitem__(self, key):
                return 1

            def __setitem__(self, key, value):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def __len__(self):
                return 0

            def __str__(self):
                return "cart"

            def total(self):
                return 0


        def make() -> Cart:
            return Cart()


        def build():
            return Cart()


        def use():
            cart = make()
            cart["apple"] = 2
            with build() as other:
                other["pear"]
            label = f"{cart}"
            return len(cart), build().total()


        class Shop:
            cart: Cart

            def checkout(self):
                return self.cart.total()
    """})
    found = calls(graph)
    for method in ("__setitem__", "__enter__", "__exit__", "__getitem__", "__len__", "__str__", "total"):
        assert ("store:use", f"store:Cart.{method}") in found, method
    assert ("store:Shop.checkout", "store:Cart.total") in found  # class-level annotation


def test_functions_held_in_variables_and_calls_by_name(tmp_path):
    graph = analyze(tmp_path, {"headers.py": """
        def parse_date(value):
            return value


        class DateHeader:
            parser = staticmethod(parse_date)

            def parse(self, value):
                return self.parser(value)


        class Message:
            def get_content_type(self):
                return "text/plain"


        def walk(part):
            part.split()  # a str method: never matched by name
            return part.get_content_type()
    """})
    graph_edges = {(e["source"], e["target"]): e for e in graph["edges"] if e["kind"] == "calls"}
    assert ("headers:DateHeader.parse", "headers:parse_date") in graph_edges
    guess = graph_edges[("headers:walk", "headers:Message.get_content_type")]
    assert guess["by_name"] is True
    assert "by_name" not in graph_edges[("headers:DateHeader.parse", "headers:parse_date")]


def test_no_calls_by_name_through_modules(tmp_path):
    graph = analyze(tmp_path, {"tools.py": """
        import functools
        import json
        import os
        import struct
        import subprocess
        import sys
        from os import path
        from third_party import client


        class Runner:
            def run(self):
                pass

            def dumps(self):
                pass

            def isdir(self):
                pass

            def pack(self):
                pass

            def partial(self):
                pass

            def fetch(self):
                pass

            @property
            def maxsize(self):
                return 0


        def main(x):
            subprocess.run(["ls"])
            json.dumps(x)
            os.path.isdir(x)
            path.isdir(x)
            struct.pack("i", 1)
            functools.partial(main)
            client.fetch()
            return sys.maxsize


        def other(obj):
            return obj.run()
    """})
    found = calls(graph)
    assert not {target for source, target in found if source == "tools:main"}
    assert ("tools:other", "tools:Runner.run") in found  # an unknown object, not a module


def test_a_method_matched_by_name_keeps_the_receivers_flow(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        class Handler:
            def handle(self):
                pass


        def cb(obj):
            return obj.handle
    """})
    assert ("m:cb#obj", "m:cb#return") in edges(graph, "flows_to")


def test_property_objects_are_not_getter_calls(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        class C:
            @property
            def x(self):
                return 1

            @x.setter
            def x(self, v):
                pass

            @x.deleter
            def x(self):
                pass


        def doc():
            return C.x.__doc__


        def get(c: C):
            return c.x
    """})
    found = calls(graph)
    assert ("m", "m:C.x") not in found  # @x.setter, @x.deleter
    assert ("m:doc", "m:C.x") not in found  # on the class, not an instance
    assert ("m:get", "m:C.x") in found


def test_type_checking_imports_do_not_run(tmp_path):
    graph = analyze(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/models.py": "class Model:\n    def save(self):\n        pass\n",
        "pkg/app.py": """
            from __future__ import annotations

            import typing
            from typing import TYPE_CHECKING

            if TYPE_CHECKING:
                from pkg.models import Model
            if typing.TYPE_CHECKING:
                import pkg.models
            else:
                import pkg

            def use(m: Model):
                m.save()
        """,
    })
    graph_edges = {(e["source"], e["target"]): e for e in graph["edges"] if e["kind"] == "calls"}
    assert ("pkg.app", "pkg.models") not in graph_edges
    assert ("pkg.app", "pkg") in graph_edges  # the else branch runs
    assert "by_name" not in graph_edges[("pkg.app:use", "pkg.models:Model.save")]  # the type still resolves


def test_f_string_conversions(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        class P:
            def __repr__(self):
                return "P()"

            def __str__(self):
                return "p"

            def __format__(self, spec):
                return "p"


        def r(p: P):
            return f"{p!r}"


        def a(p: P):
            return f"{p!a}"


        def s(p: P):
            return f"{p!s}"


        def plain(p: P):
            return f"{p}"
    """})
    found = {(source.rpartition(":")[2], target.rpartition(".")[2]) for source, target in calls(graph)}
    assert found == {("r", "__repr__"), ("a", "__repr__"), ("s", "__str__"), ("plain", "__format__")}


def test_the_latest_assignment_types_a_variable(tmp_path):
    graph = analyze(tmp_path, {"m.py": """
        class A:
            def m(self):
                pass


        class B:
            def m(self):
                pass

            def n(self):
                pass


        def f():
            x = A()
            x = B()
            x.m()


        def g():
            y: A = B()  # the value's class, more precise than the annotation
            y.n()
    """})
    found = calls(graph)
    assert ("m:f", "m:B.m") in found and ("m:f", "m:A.m") not in found
    assert ("m:g", "m:B.n") in found


def test_skips_directories_the_gitignore_lists(tmp_path):
    graph = analyze(tmp_path, {
        ".gitignore": "# outputs\nruns/\n/data\noutput\n*.egg-info/\nsrc/generated/\n\n",
        "app.py": "",
        "runs/r.py": "",
        "data/d.py": "",
        "sub/data/kept.py": "",  # /data is anchored to the top
        "sub/output/o.py": "",
        "x.egg-info/e.py": "",
        "src/generated/g.py": "",  # a path: too fancy, not used
    })
    files = {n["file"] for n in graph["nodes"] if n["kind"] == "module"}
    assert files == {"app.py", "sub/data/kept.py", "src/generated/g.py"}


def test_star_import_cycles_are_fast(tmp_path):
    names = ["a", "b", "c", "d"]
    files = {"pkg/__init__.py": ""}
    for name in names:
        stars = "".join(f"from .{other} import *\n" for other in names if other != name)
        files[f"pkg/{name}.py"] = stars + f"\ndef f_{name}(x):\n    return len(x) + g(x)\n"
    files["pkg/a.py"] += "\ndef g(x):\n    return x\n"
    start = time.perf_counter()
    graph = analyze(tmp_path, files)
    assert time.perf_counter() - start < 1
    assert ("pkg.d:f_d", "pkg.a:g") in calls(graph)  # still found through the star imports
