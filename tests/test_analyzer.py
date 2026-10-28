"""Static analyzer: each test builds a tiny project and checks the edges the spec calls for."""

import json
import textwrap
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
