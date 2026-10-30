import json
import os
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
    assert set(tools) == {"run", "runs", "summary", "exceptions", "calls", "call_tree", "frame", "find", "neighbors"}
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
    assert "checkout(items=[{'price': 3, 'qty': 2}, {'price': 5}], budget=20) -> 9" in calls
    assert "checkout(items=[{'price': 30}], budget=20) !! ValueError: over budget: 30 > 20" in calls

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
def test_a_failing_test_under_pytest(client, project):
    (project / "test_cart.py").write_text(textwrap.dedent("""
        from cart import total

        def test_total():
            assert total([{"price": 2, "qty": 3}]) == 5
    """))
    text = client.ok("run", command=f"{PYTHON} -m pytest -q -p no:cacheprovider test_cart.py")
    assert "exit status 1" in text and "1 failed" in text
    assert "AssertionError" in text and "test_cart:test_total" in text
    assert "total(items=[{'price': 2, 'qty': 3}]) -> 6" in client.ok("calls", function="total")


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
    assert "checkout(items=[{'price': 30}]" in client.ok("calls", function="checkout", process=child)
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
    text, error = client.tool("run", command="no-such-program-here")
    assert error and "could not start" in text
    text, error = client.tool("summary", run="19990101-000000")
    assert error and "no run 19990101-000000" in text


@needs_monitoring
def test_python_that_runs_no_project_code_says_so(client, tmp_path):
    text = client.ok("run", command=f"{PYTHON} -c pass")
    assert "no project code ran" in text
