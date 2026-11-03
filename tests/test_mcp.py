import json
import os
import re
import shlex
import subprocess
import sys
import textwrap

import pytest

from graphcheck import REPO

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")
PYTHON = shlex.quote(sys.executable)


class Client:
    """Talks to `luciole mcp` over stdio, as an MCP client does."""

    def __init__(self, root):
        env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "luciole", "mcp", "--root", str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, text=True,
        )
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}) + "\n")
        self.process.stdin.flush()
        reply = json.loads(self.process.stdout.readline())
        assert reply["id"] == self.next_id
        return reply

    def tool(self, _tool, **arguments):
        """The tool's text, and whether it is an error."""
        result = self.request("tools/call", {"name": _tool, "arguments": arguments})["result"]
        return result["content"][0]["text"], result["isError"]

    def ok(self, _tool, **arguments):
        text, error = self.tool(_tool, **arguments)
        assert not error, text
        return text

    def close(self):
        self.process.stdin.close()
        self.process.wait(10)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "cart.py").write_text(textwrap.dedent("""
        def price_of(item):
            return item["price"] * item.get("qty", 1)


        def total(items):
            return sum(price_of(item) for item in items)


        def checkout(items, budget):
            due = total(items)
            if due > budget:
                raise ValueError(f"over budget: {due} > {budget}")
            return budget - due


        if __name__ == "__main__":
            print(checkout([{"price": 3, "qty": 2}, {"price": 5}], 20))
            print(checkout([{"price": 30}], 20))
    """))
    return tmp_path


@pytest.fixture
def client(project):
    client = Client(project)
    client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
    yield client
    client.close()


def test_initialize_and_list_tools(project):
    client = Client(project)
    reply = client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}})
    assert reply["result"]["protocolVersion"] == "2024-11-05"
    assert reply["result"]["capabilities"] == {"tools": {}}
    assert "run" in reply["result"]["instructions"]
    tools = {tool["name"]: tool for tool in client.request("tools/list")["result"]["tools"]}
    assert set(tools) == {"run", "runs", "summary", "exceptions", "traceback", "calls", "call_tree", "frame", "variable_history", "origin", "find", "neighbors"}
    assert tools["run"]["inputSchema"]["required"] == ["command"]
    assert client.request("no/such/method")["error"]["code"] == -32601
    client.close()


@needs_monitoring
def test_run_records_calls_values_and_the_exception(client):
    text = client.ok("run", command=f"{PYTHON} cart.py")
    assert "exit status 1" in text and "ValueError: over budget: 30 > 20" in text  # the output
    assert "recorded 1 process:" in text
    assert "exceptions nothing in the project handled" in text and "raised in cart:checkout:13" in text

    calls = client.ok("calls", function="checkout")
    assert calls.startswith("2 calls of cart:checkout, from 2 call sites\n  same in all 2 calls shown: budget=20\n")
    assert "\n  from cart:18\n    #2 -> 9  (items=[{'price': 3, 'qty': 2}, {'price': 5}])   " in calls  # the result first
    assert "\n  from cart:19\n    #6 !! ValueError: over budget: 30 > 20  (items=[{'price': 30}])   " in calls
    detail = client.ok("calls", function="checkout", detail=True)
    assert "checkout(items=[{'price': 3, 'qty': 2}, {'price': 5}], budget=20) -> 9" in detail
    assert "checkout(items=[{'price': 30}], budget=20) !! ValueError: over budget: 30 > 20" in detail

    post_mortem = client.ok("traceback")
    assert "in cart:checkout (#6), line 13: raised here" in post_mortem
    assert "budget = 20" in post_mortem and "due = 30" in post_mortem and "not handled" in post_mortem

    exceptions = client.ok("exceptions")
    assert "ValueError: over budget: 30 > 20" in exceptions and "not handled" in exceptions

    tree = client.ok("call_tree")
    assert "cart:price_of ×2" in tree and "[raise]" in tree

    frame = int(calls.split("#", 2)[1].split()[0])  # the first call of checkout
    detail = client.ok("frame", frame=frame)
    assert detail.startswith(f"#{frame} checkout(") and "cart:total" in detail

    runs = client.ok("runs")
    assert len(runs.splitlines()) == 1 and "(exit status 1, 1 process recorded)" in runs


@needs_monitoring
def test_variable_history_asks_for_a_scoped_run_first(client):
    client.ok("run", command=f"{PYTHON} cart.py")
    text, error = client.tool("variable_history", function="checkout")
    assert error and "no variable history for cart:checkout in this run" in text and "recorded at level 1" in text
    assert 'run` the same command again with scope: ["cart:checkout"]' in text

    text = client.ok("run", command=f"{PYTHON} cart.py", scope="checkout")
    assert "variable history (level 2) for checkout" in text
    history = client.ok("variable_history", function="checkout")
    assert history.startswith("2 writes in 2 calls of cart:checkout\n")
    assert "checkout(items=[{'price': 3, 'qty': 2}, {'price': 5}], budget=20)" in history
    assert "line 11   due = 11" in history and "line 11   due = 30" in history  # the second call raises after it
    assert "line 14   return 9\n" in history and history.endswith("line 13   raised ValueError: over budget: 30 > 20")  # how each call ended
    text, error = client.tool("variable_history", function="checkout", variable="nope")
    assert not error and "nope was never assigned" in text
    text, error = client.tool("variable_history", function="total")
    assert error and "its scope was checkout" in text

    text, error = client.tool("run", command=f"{PYTHON} cart.py", level=2)
    assert error and "give them as a scope" in text
    text, error = client.tool("run", command=f"{PYTHON} cart.py", level=0, scope=["checkout"])
    assert error and "which is level 2" in text


@needs_monitoring
def test_a_failing_test_under_pytest(client, project):
    (project / "test_cart.py").write_text(textwrap.dedent("""
        from cart import total

        def test_total():
            assert total([{"price": 2, "qty": 3}]) == 5
    """))
    text = client.ok("run", command=f"{PYTHON} -m pytest -q -p no:cacheprovider test_cart.py")
    assert "exit status 1" in text and "1 failed" in text
    assert "AssertionError" in text and "test_cart:test_total" in text
    assert "-> 6  (items=[{'price': 2, 'qty': 3}])" in client.ok("calls", function="total")


@needs_monitoring
def test_child_processes_are_recorded(client, project):
    (project / "parent.py").write_text(textwrap.dedent(f"""
        import subprocess, sys

        def spawn():
            subprocess.run([sys.executable, "cart.py"])

        spawn()
    """))
    text = client.ok("run", command=f"{PYTHON} parent.py")
    assert "recorded 2 processes:" in text
    assert "raised in cart:checkout:13" in text  # in the child
    child = int(text.split("pid ")[2].split(":")[0])
    assert "(items=[{'price': 30}])" in client.ok("calls", function="checkout", process=child)
    assert "no calls of checkout" in client.tool("calls", function="checkout")[0]  # the parent's


@needs_monitoring
def test_a_program_that_runs_too_long_is_stopped(client, project):
    (project / "slow.py").write_text("import time\n\ndef wait():\n    time.sleep(60)\n\nwait()\n")
    text = client.ok("run", command=f"{PYTHON} slow.py", timeout=1)
    assert "stopped after 1 s (timeout)" in text
    if os.name == "posix":  # Ctrl+C first; on Windows the program is killed
        assert "KeyboardInterrupt" in text and "pid " in text


def test_graph_questions_need_no_run(client):
    assert "function cart:price_of  cart.py:2" in client.ok("find", name="price_of")
    around = client.ok("neighbors", function="total")
    assert "called by:\n  cart:checkout  (line 11)" in around
    assert "calls:\n  cart:price_of  (line 7)" in around
    assert "items <- cart:checkout#items" in around
    assert "return value goes to:\n  cart:checkout#due" in around


def test_mistakes_are_explained(client):
    text, error = client.tool("calls", function="checkout")
    assert error and "no runs yet" in text
    text, error = client.tool("neighbors", function="nope")
    assert error and "no function named nope" in text
    text, error = client.tool("run", command="no-such-program-here --flag")
    assert error and "could not start no-such-program-here: it is not on PATH" in text and "`python -m no_such_program_here --flag`" in text
    text, error = client.tool("summary", run="19990101-000000")
    assert error and "no run 19990101-000000" in text


@needs_monitoring
def test_python_that_runs_no_project_code_says_so(client, tmp_path):
    text = client.ok("run", command=f"{PYTHON} -c pass")
    assert "no project code ran" in text


@needs_monitoring
def test_run_takes_variables_before_the_command(client, project):
    (project / "lib").mkdir()
    (project / "lib" / "helper.py").write_text("def twice(n):\n    return n * 2\n")
    (project / "use.py").write_text("import os\nfrom helper import twice\n\ndef main():\n    print(twice(int(os.environ['N'])))\n\nmain()\n")
    text = client.ok("run", command=f"PYTHONPATH=lib N=21 {PYTHON} use.py")
    assert "exit status 0" in text and "42" in text
    assert "use:main" in client.ok("call_tree")  # still recorded: the recorder's folder kept its place



@pytest.fixture
def shop(project):
    """A program whose functions are called often, from several places, with chained errors."""
    (project / "shop.py").write_text(textwrap.dedent("""
        import time


        class Catalog:
            def __init__(self, rows):
                self.rows = {row["sku"]: row for row in rows}

            def lookup(self, key, note="a long note about this catalog, " * 4):
                return self.rows.get(key)

            def price(self, key):
                row = self.lookup(key)
                if row is None:
                    raise KeyError(key)
                return row["price"]


        class OrderError(Exception):
            pass


        def line_total(catalog, key, qty):
            try:
                return catalog.price(key) * qty
            except KeyError as error:
                raise ValueError(f"unknown sku {key!r}") from error


        def tiny(n):
            return n + 1


        def small(n):
            return n * 2


        def little(n):
            return -n


        def warm_up(catalog):
            for _ in range(30):
                catalog.lookup("A1")
            for n in range(5):
                tiny(n)
            small(1)
            little(1)
            time.sleep(0.1)  # the time that matters


        def main():
            catalog = Catalog([{"sku": "A1", "price": 3}, {"sku": "B2", "price": 5}])
            warm_up(catalog)
            print(line_total(catalog, "B2", 2))
            try:
                line_total(catalog, "ZZ", 1)
            except ValueError as error:
                raise OrderError("order failed") from error


        main()
    """))
    return project


@needs_monitoring
def test_calls_are_grouped_by_caller_with_constant_arguments_said_once(client, shop):
    client.ok("run", command=f"{PYTHON} shop.py")
    calls = client.ok("calls", function="Catalog.lookup")
    lines = calls.splitlines()
    assert lines[0] == "32 calls of shop:Catalog.lookup, from 2 call sites"
    assert lines[1].startswith("  same in all 20 calls shown: self=<Catalog object …") and "key='A1', note='a long note about this catalog, a long note" in lines[1]
    assert lines[1].endswith("…")  # cut short
    assert lines[2] == "  from shop:warm_up:44 ×30"
    assert re.match(r"    #\d+\.\.#\d+ ×20 -> \{'sku': 'A1', 'price': 3\}   [\d.]+ .s in all, caller #\d+$", lines[3])  # consecutive calls that read the same
    assert lines[4] == "  also from shop:Catalog.price:13 ×2 (not shown here)"
    assert lines[5] == "  ... 12 more (limit, offset)"
    assert lines[6].startswith("values cut short: `frame` with frame=")

    mine = client.ok("calls", function="lookup", caller="Catalog.price")
    assert mine.startswith("2 calls of shop:Catalog.lookup from shop:Catalog.price (32 in all)\n")
    assert "-> {'sku': 'B2', 'price': 5}  (key='B2')" in mine and "-> None  (key='ZZ')" in mine  # `key` is not a secret
    text, error = client.tool("calls", function="lookup", caller="main")
    assert error and "no calls of shop:Catalog.lookup from main; it was called from shop:warm_up:44 ×30, shop:Catalog.price:13 ×2" in text


@needs_monitoring
def test_call_tree_folds_small_calls_and_focuses_on_a_function(client, shop):
    client.ok("run", command=f"{PYTHON} shop.py")
    tree = client.ok("call_tree", depth=6)
    assert "shop:tiny" not in tree and re.search(r"\n      … \d+ small calls: .*tiny ×5, (small, little|little, small)  \(", tree)
    assert "shop:Catalog.price  #" in tree and "[raise]" in tree  # what raised is never folded
    assert tree.endswith("next: `call_tree` with focus=FUNCTION keeps only the paths to its calls; frame=#N opens one call")

    focused = client.ok("call_tree", focus="Catalog.price")
    lines = focused.splitlines()
    assert lines[0] == "2 calls of shop:Catalog.price: the paths to all, from the top (<== marks them; other calls are counted, not shown)"
    assert lines[1].startswith("shop  #1  ") and "(+2 other calls)" in lines[1]  # the classes' bodies
    assert "warm_up" not in focused and "tiny" not in focused
    assert sum(line.endswith("<==") for line in lines) == 2
    assert any(line.strip().startswith("shop:Catalog.lookup  #") for line in lines)  # one level below each
    warm_up = int(re.search(r"#(\d+) ", client.ok("calls", function="warm_up")).group(1))
    below = client.ok("call_tree", focus="tiny", frame=warm_up)
    assert below.startswith(f"5 calls of shop:tiny; below #{warm_up}: the paths to 5 calls") and "<==" in below
    assert client.ok("call_tree", focus="Catalog.price", frame=warm_up) == f"2 calls of shop:Catalog.price, none below #{warm_up}"
    text, error = client.tool("call_tree", focus="nope")
    assert error and "no calls of nope" in text


@needs_monitoring
def test_traceback_starts_from_the_original_error(client, shop):
    text = client.ok("run", command=f"{PYTHON} shop.py")
    assert "__main__.OrderError: order failed  raised in shop:main:59" in text
    assert text.endswith("next: `traceback` shows the original error, the frames it went through and their locals")
    post_mortem = client.ok("traceback")
    lines = post_mortem.splitlines()
    assert lines[0] == "!1 KeyError: 'ZZ'"
    assert lines[1] == "  the original error of a chain of 3, outermost first (`traceback` with exception=N shows another):"
    assert lines[2].startswith("    !3 __main__.OrderError: order failed   raised in shop:main:59, not handled")
    assert lines[3].startswith("    !2 ValueError: unknown sku 'ZZ'   raised in shop:line_total:27, handled in shop:main")
    assert lines[4].startswith("    !1 KeyError: 'ZZ'   raised in shop:Catalog.price:15") and lines[4].endswith("<== this one")
    assert lines[5].startswith("  in shop:Catalog.price (#") and lines[5].endswith("line 15: raised here")
    assert "      key = 'ZZ'" in lines and "      row = None" in lines
    assert any(line.startswith("  handled in shop:line_total (#") and line.endswith("which raised !2 from it") for line in lines)
    assert lines[-1].startswith("next: `calls` function=shop:Catalog.price")
    outer = client.ok("traceback", exception=3)
    assert outer.startswith("!3 __main__.OrderError: order failed\n  the outermost of a chain of 3") and "  not handled" in outer


@needs_monitoring
def test_run_answers_stay_short(client, project):
    (project / "noisy.py").write_text("for i in range(5000):\n    print(f'line {i} ' + 'x' * (i % 200))\n")
    text = client.ok("run", command=f"{PYTHON} noisy.py")
    assert len(text) < 4000
    assert "  line 0 \n" in text and text.count("line 4999 ") == 1
    assert re.search(r"\.\.\. \d+ lines cut \(\d+ KB\): the whole output is in .*output\.txt", text)


def test_package_manager_wrappers_are_not_run(client, project):
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").write_text("")
    text, error = client.tool("run", command="uv run --with rich pytest -x tests")
    assert error and "not run: `uv run` may build or sync an environment first" in text
    assert "Run `.venv/bin/python -m pytest -x tests` instead" in text
    text, error = client.tool("run", command="poetry run python cart.py")
    assert error and "Run `.venv/bin/python cart.py` instead" in text
    assert "no runs yet" in client.tool("runs")[0]  # nothing was started


def test_a_missing_command_names_the_project_script(client, project):
    (project / "pyproject.toml").write_text('[project]\nname = "cart"\n\n[project.scripts]\ncart-tool = "cart:checkout"\n')
    text, error = client.tool("run", command="cart-tool 1")
    assert error and "could not start cart-tool: it is not on PATH. It is the project's script for cart:checkout" in text
    assert "from cart import checkout; sys.argv[0] = " in text
