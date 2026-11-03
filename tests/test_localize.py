"""Fault localization (localize.py): suspects and compare, on small recorded test runs."""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from graphcheck import REPO
from luciole.localize import align, compare, compare_tests, is_test_node, suspects
from luciole.localize_text import render_compare, render_suspects
from luciole.runs import processes_of
from luciole.trace import Trace

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")

SHOP = """
    def price_of(item):
        return item["price"] * item.get("qty", 1)


    def subtotal(items):
        return sum(price_of(item) for item in items)


    def tax(amount, rate):
        return round(amount * rate)


    def shipping(weight):
        if weight > 10:
            return 15
        return 5


    def apply_coupon(amount, code):
        if code == "HALF":
            return amount // 3
        return amount


    def checkout(items, code=None, weight=1):
        amount = subtotal(items)
        if code:
            amount = apply_coupon(amount, code)
        return amount + shipping(weight)
"""

TESTS = {
    "tests/test_basic.py": """
        from shop import checkout, tax


        def test_plain():
            assert checkout([{"price": 10}]) == 15


        def test_heavy():
            assert checkout([{"price": 10}], weight=20) == 25


        def test_tax():
            assert tax(100, 0.2) == 20
    """,
    "tests/test_coupon.py": """
        from shop import checkout


        def test_half():
            assert checkout([{"price": 10}], code="HALF") == 10


        def test_half_two():
            assert checkout([{"price": 20}], code="HALF") == 15


        def test_other_code():
            assert checkout([{"price": 10}], code="NONE") == 15
    """,
}

PARAMETRIZED = {
    "cart.py": """
        def price_of(item):
            return item["price"] * item.get("qty", 1)


        def discount(subtotal, count):
            if count > 2:
                return subtotal * 0.2
            return 0


        def total(items):
            subtotal = sum(price_of(item) for item in items)
            return round(subtotal - discount(subtotal, len(items)))
    """,
    "tests/test_cart.py": """
        import pytest

        from cart import total


        @pytest.mark.parametrize("n, expected", [(1, 10), (3, 27)])
        def test_many(n, expected):
            assert total([{"price": 10}] * n) == expected
    """,
}


def env(**extra):
    variables = {key: value for key, value in os.environ.items() if not key.startswith("LUCIOLE_") or key == "LUCIOLE_HOME"}
    variables["PYTHONPATH"] = str(REPO / "src")
    variables.update(extra)
    return variables


def write(root, files):
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    (root / "pytest.ini").write_text("[pytest]\n")
    sys.path_importer_cache.clear()


@pytest.fixture
def shop(tmp_path):
    root = tmp_path / "shop"
    write(root, {"shop.py": SHOP, **TESTS})
    return root


def record(root, folder, *args):
    """pytest --luciole into `root/folder`: the run folder's traces."""
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "--luciole", "--luciole-dir", folder, "--luciole-keep=always", *args]
    result = subprocess.run(command, cwd=root, env=env(), capture_output=True, text=True, timeout=120)
    assert "recording kept" in result.stdout, result.stdout + result.stderr
    [run] = list((root / folder).iterdir())
    return processes_of(run)


def luciole(root, *args):
    result = subprocess.run([sys.executable, "-m", "luciole", *map(str, args)], cwd=root, env=env(), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def graph_of(root):
    from luciole.analyzer import build_graph

    return build_graph(root)


def test_test_code_is_told_apart():
    assert is_test_node("tests.test_cart:test_one") and is_test_node("test_cart:helper") and is_test_node("conftest:db")
    assert is_test_node("pkg.tests.helpers:make") and is_test_node("cart_test:test_x")
    assert not is_test_node("cart:total")
    assert not is_test_node("attestation:check")


def test_alignment_tolerates_inserted_calls():
    ops = align(["a", "b", "c"], ["a", "x", "b", "c"])
    assert ops == [("match", 0, 0), ("b_only", 1, 1), ("match", 1, 2), ("match", 2, 3)]
    assert align(["a", "b"], ["a"]) == [("match", 0, 0), ("a_only", 1, 1)]


@needs_monitoring
def test_the_buggy_function_ranks_first(shop):
    paths = record(shop, "rec")
    found = suspects(paths, graph_of(shop))
    assert (found["source"], found["failed"], found["passed"], found["method"]) == ("recorded", 2, 4, "ochiai")
    first, second = found["suspects"][:2]
    assert first["node"] == "shop:apply_coupon" and first["rank"] == 1 and second["rank"] == 2
    assert (first["failed"], first["passed"]) == (2, 1) and round(first["score"], 3) == round(2 / (2 * 3) ** 0.5, 3)
    assert (first["file"], first["line"]) == ("shop.py", 20)
    assert not any(is_test_node(item["node"]) for item in found["suspects"])
    assert first["example"]["kind"] == "different"
    assert first["example"]["failing"]["call"]["result"]["repr"] == "3"
    text = render_suspects(found)
    lines = text.splitlines()
    assert lines[0] == "suspects from 6 tests (2 failed, 4 passed; outcomes recorded by pytest --luciole)"
    assert lines[3].split() == ["1", "0.82", "2/2", "1/4", "shop:apply_coupon", "shop.py:20"]
    assert "a call no passing test made: #" in lines[4] and "apply_coupon(amount=10, code='HALF') -> 3" in lines[4]
    assert "in a passing test: #" in lines[5] and "code='NONE') -> 10" in lines[5]


@needs_monitoring
def test_compare_a_failing_test_with_the_closest_passing_one(shop):
    paths = record(shop, "rec")
    found = compare_tests(paths)
    assert found["test_a"]["nodeid"] == "tests/test_coupon.py::test_half"
    assert found["test_b"]["nodeid"] == "tests/test_coupon.py::test_other_code"
    assert found["picked"].startswith("the passing test whose functions are closest (Jaccard 1.00")
    divergence = found["divergence"]
    assert (divergence["kind"], divergence["node"], divergence["same_arguments"]) == ("result", "shop:apply_coupon", False)
    assert [step["node"] for step in divergence["path"]] == ["test_coupon:test_half", "shop:checkout", "shop:apply_coupon"]
    assert divergence["path"][1]["arguments_differ"] == ["code"]
    text = render_compare(found, hints="mcp")
    assert "first divergence (4 calls compared): apply_coupon returned something else, its arguments differ too" in text
    assert "variable=return" in text


@needs_monitoring
def test_a_parametrized_test_diverges_at_the_first_different_return(tmp_path):
    root = tmp_path / "cart"
    write(root, PARAMETRIZED)
    paths = record(root, "rec")
    found = compare_tests(paths)
    assert found["test_a"]["nodeid"] == "tests/test_cart.py::test_many[3-27]"
    assert found["test_b"]["nodeid"] == "tests/test_cart.py::test_many[1-10]"
    assert found["picked"] == "the same test function with other parameters"
    divergence = found["divergence"]
    assert (divergence["kind"], divergence["node"]) == ("result", "cart:discount")
    assert (divergence["call_a"]["result"]["repr"], divergence["call_b"]["result"]["repr"]) == ("6.0", "0")
    assert divergence["path"][1]["extra_calls"] == {"a:cart:price_of": 2}  # a longer loop, not a path of its own
    text = render_compare(found)
    assert "  A #" in text and "discount(subtotal=30, count=3) -> 6.0" in text
    assert "(A made 2 more calls of price_of)" in text
    assert "inputs: n=3 vs 1, expected=27 vs 10" in text


@needs_monitoring
def test_the_same_test_in_a_passing_run(shop, tmp_path):
    failing = record(shop, "before")
    (shop / "shop.py").write_text(textwrap.dedent(SHOP).replace("amount // 3", "amount // 2  # fixed"))
    passing = record(shop, "after")
    found = compare_tests(failing, passing)
    assert found["picked"] == "the same test in the other run"
    divergence = found["divergence"]
    assert (divergence["kind"], divergence["node"], divergence["same_arguments"]) == ("result", "shop:apply_coupon", True)
    assert found["first_arguments"] is None  # the same inputs all the way down
    assert "apply_coupon returned something else, with the same arguments" in render_compare(found)
    # Two frames given: the same answer.
    with Trace(failing[0]) as a, Trace(passing[0]) as b:
        again = compare(a, found["test_a"]["frame"], b, found["test_b"]["frame"])
    assert again["divergence"]["a"] == divergence["a"]
    # Nothing differs between a call and itself.
    with Trace(passing[0]) as b:
        same = compare(b, found["test_b"]["frame"], b, found["test_b"]["frame"])
    assert same["divergence"] is None and "no divergence" in render_compare(same)


@needs_monitoring
def test_no_passing_tests(shop):
    paths = record(shop, "rec", "-k", "half")
    found = suspects(paths)
    assert found["method"] == "no_passing" and found["passed"] == 0
    top = [item["node"] for item in found["suspects"] if item["rank"] == 1]
    assert "shop:apply_coupon" in top and "shop:checkout" not in top  # checkout has two callers
    text = render_suspects(found)
    assert "no passing test: Ochiai cannot tell functions apart" in text
    with pytest.raises(LookupError, match="no passing test"):
        compare_tests(paths)


@needs_monitoring
def test_no_failing_tests(shop):
    paths = record(shop, "rec", "-k", "plain or heavy")
    found = suspects(paths)
    assert (found["method"], found["failed"], found["passed"], found["suspects"]) == ("no_failing", 0, 2, [])
    assert render_suspects(found).endswith("no test failed, nothing to localize")


@needs_monitoring
def test_outcomes_inferred_without_the_plugin(shop, tmp_path):
    path = tmp_path / "run.sqlite"
    command = [sys.executable, "-m", "luciole", "run", "--root", str(shop), "--trace", str(path), "-m", "pytest", "-p", "no:cacheprovider", "-q"]
    subprocess.run(command, cwd=shop, env=env(), capture_output=True, text=True, timeout=120)
    found = suspects([path])
    assert (found["source"], found["failed"], found["passed"]) == ("inferred", 2, 4)
    assert found["suspects"][0]["node"] == "shop:apply_coupon"
    assert "outcomes inferred from the test functions' calls" in render_suspects(found)


@needs_monitoring
def test_level_2_ranks_lines(shop):
    paths = record(shop, "rec", "--luciole-scope", "checkout")
    found = suspects(paths)
    checkout = next(item for item in found["suspects"] if item["node"] == "shop:checkout")
    assert checkout.get("lines") and checkout["lines"][0]["line"] == 29  # amount = apply_coupon(...): failing tests only


@needs_monitoring
def test_cli(shop):
    paths = record(shop, "rec")
    run = paths[0].parent
    text = luciole(shop, "trace", run, "suspects", "--top", "3")
    assert "shop:apply_coupon  shop.py:20" in text and text.count("\n") < 20
    data = json.loads(luciole(shop, "trace", run, "suspects", "--json"))
    assert data["suspects"][0]["node"] == "shop:apply_coupon"
    text = luciole(shop, "trace", run, "compare")
    assert text.startswith("A: #") and "apply_coupon returned something else" in text
    assert f"next: luciole trace {paths[0]} origin - return --frame " in text
    data = json.loads(luciole(shop, "trace", run, "compare", "--json"))
    frames = data["test_a"]["frame"], data["test_b"]["frame"]
    text = luciole(shop, "trace", paths[0], "compare", *frames)
    assert "apply_coupon returned something else" in text
