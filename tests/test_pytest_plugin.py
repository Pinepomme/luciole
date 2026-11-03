"""The pytest plugin (pytest --luciole): post-mortem recordings of test runs."""

import importlib.metadata
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from graphcheck import REPO
from luciole.outcomes import run_tests
from luciole.runs import processes_of, runs_folder
from luciole.trace import Trace

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")

PROJECT = {
    "pytest.ini": "[pytest]\n",
    "cart.py": """
        def price_of(item):
            return item["price"] * item.get("qty", 1)


        def total(items):
            discount = 0.1 if len(items) > 2 else 0
            subtotal = sum(price_of(item) for item in items)
            return round(subtotal * (1 - discount))
    """,
    "tests/test_cart.py": """
        import pytest

        from cart import total


        def test_one():
            assert total([{"price": 10}]) == 10


        def test_three():
            items = [{"price": 10}, {"price": 10, "qty": 2}, {"price": 1}]
            expected = 31
            assert total(items) == expected


        @pytest.fixture
        def database():
            raise RuntimeError("no database")


        def test_db(database):
            pass


        @pytest.mark.skip(reason="later")
        def test_later():
            pass


        @pytest.mark.xfail(reason="known")
        def test_known():
            assert total([]) == 1
    """,
}


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    for name, source in PROJECT.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    sys.path_importer_cache.clear()
    return root


def env(**extra):
    variables = {key: value for key, value in os.environ.items() if not key.startswith("LUCIOLE_") or key == "LUCIOLE_HOME"}
    variables["PYTHONPATH"] = str(REPO / "src")
    variables.update(extra)
    return variables


def pytest_run(root, *args, **extra):
    command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args]
    return subprocess.run(command, cwd=root, env=env(**extra), capture_output=True, text=True, timeout=120)


def runs_of(root):
    folder = runs_folder(root.resolve())
    return sorted(path for path in folder.iterdir() if path.is_dir()) if folder.is_dir() else []


def test_the_plugin_is_registered():
    assert any(entry.value == "luciole.pytest_plugin" for entry in importlib.metadata.entry_points(group="pytest11")), "run `uv sync`"


def test_inert_without_the_flag(project):
    (project / "conftest.py").write_text(textwrap.dedent("""
        import json, sys

        def pytest_unconfigure(config):
            tools = [sys.monitoring.get_tool(tool) for tool in range(6)] if hasattr(sys, "monitoring") else []
            modules = sorted(name for name in sys.modules if name.startswith("luciole"))
            with open("state.json", "w") as file:
                json.dump({"tools": tools, "modules": modules, "env": "LUCIOLE_TRACE" in __import__("os").environ}, file)
    """))
    result = pytest_run(project, "-q")
    assert result.returncode == 1
    assert "luciole" not in result.stdout + result.stderr
    state = json.loads((project / "state.json").read_text())
    assert "luciole" not in state["tools"] and not state["env"]
    assert state["modules"] == ["luciole", "luciole.pytest_plugin"]  # loaded by pytest, nothing more
    assert not runs_of(project)


@needs_monitoring
def test_a_failing_run_is_kept_with_outcomes_and_locals(project):
    result = pytest_run(project, "--luciole")
    assert result.returncode == 1, result.stdout + result.stderr
    [folder] = runs_of(project)
    assert f"luciole: recording at level 1 into {folder}" in result.stdout
    assert f"luciole: recording kept in {folder}: luciole trace {folder} tests --failed" in result.stdout
    meta = json.loads((folder / "command.json").read_text())
    assert meta["by"] == "pytest" and meta["command"] == ["pytest", "-p", "no:cacheprovider", "--luciole"] and meta["exit_status"] == 1
    [path] = processes_of(folder)
    with Trace(path) as trace:
        assert trace.run()["exit_status"] == 1
        tests = {test["nodeid"].split("::")[1]: test for test in trace.tests()}
        assert {name: (test["outcome"], test["phase"]) for name, test in tests.items()} == {
            "test_one": ("passed", "call"),
            "test_three": ("failed", "call"),
            "test_db": ("error", "setup"),
            "test_later": ("skipped", "setup"),
            "test_known": ("xfail", "call"),
        }
        three = tests["test_three"]
        assert three["node"] == "test_cart:test_three" and three["attempt"] == 1
        assert three["message"].startswith("AssertionError: assert 28 == 31")
        assert trace.frame(three["frame"])["node"] == "test_cart:test_three"
        found = trace.traceback(three["exception"]["id"])
        [frame] = found["frames"]
        assert frame["node"] == "test_cart:test_three"
        assert {name: value["repr"] for name, value in frame["locals"].items()} == {"items": "[{'price': 10}, {'price': 10, 'qty': 2}, {'price': 1}]", "expected": "31"}
        assert tests["test_db"]["frame"] is None and tests["test_db"]["exception"]["type"] == "RuntimeError"
        assert tests["test_later"]["message"] == "Skipped: later"
        assert [test["nodeid"].split("::")[1] for test in trace.tests(failed=True)] == ["test_three", "test_db"]
    cli = subprocess.run([sys.executable, "-m", "luciole", "trace", str(folder), "tests", "--failed"], env=env(), capture_output=True, text=True, timeout=60)
    assert cli.returncode == 0, cli.stderr
    assert cli.stdout.splitlines()[0] == "5 tests: 1 failed, 1 error, 1 passed, 1 skipped, 1 xfail"
    assert f"failed  tests/test_cart.py::test_three  (call)  #{three['frame']}  !{three['exception']['id']}" in cli.stdout
    assert "test_one" not in cli.stdout


@needs_monitoring
def test_a_passing_run_is_deleted_unless_kept(project):
    result = pytest_run(project, "--luciole", "-k", "test_one")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "luciole: every test passed: recording deleted (--luciole-keep=always keeps it)" in result.stdout
    assert not runs_of(project)
    result = pytest_run(project, "-k", "test_one", "--luciole-keep=always", LUCIOLE_PYTEST="1")
    assert result.returncode == 0, result.stdout + result.stderr
    [folder] = runs_of(project)
    assert "luciole: recording kept in" in result.stdout
    found = run_tests(processes_of(folder))
    assert found["counts"] == {"passed": 1} and json.loads((folder / "command.json").read_text())["exit_status"] == 0


@needs_monitoring
def test_options_are_checked(project):
    result = pytest_run(project, "--luciole", "--luciole-level", "2")
    assert result.returncode == 4 and "--luciole: level 2 records the variables of chosen functions only" in result.stderr
    result = pytest_run(project, "--luciole", "--luciole-scope", "total", "-k", "test_three")
    assert result.returncode == 1
    [folder] = runs_of(project)
    with Trace(processes_of(folder)[0]) as trace:
        assert trace.run()["level"] == 2
        assert [write["name"] for write in trace.history("total")["frames"][0]["writes"]] == ["discount", "subtotal"]


@needs_monitoring
def test_child_processes_are_recorded(project):
    (project / "tests" / "test_child.py").write_text(textwrap.dedent("""
        import subprocess, sys

        def test_child():
            code = "from cart import total; print(total([{'price': 2}]))"
            assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip() == "3"
    """))
    result = pytest_run(project, "--luciole", "tests/test_child.py")
    assert result.returncode == 1
    [folder] = runs_of(project)
    processes = processes_of(folder)
    assert len(processes) == 2
    with Trace(processes[1]) as child:
        assert child.calls("total")["total"] == 1 and child.tests() == []
    assert "LUCIOLE_TRACE" not in os.environ


@needs_monitoring
@pytest.mark.skipif(importlib.util.find_spec("xdist") is None, reason="pytest-xdist is not installed")
def test_xdist_workers_record_their_tests(project):
    result = pytest_run(project, "--luciole", "-n", "2")
    assert result.returncode == 1, result.stdout + result.stderr
    [folder] = runs_of(project)
    found = run_tests(processes_of(folder))
    assert found["counts"] == {"failed": 1, "error": 1, "passed": 1, "skipped": 1, "xfail": 1}
    assert found["recorded"] == 2  # the workers; the controller runs no tests and records nothing
    assert found["tests"][0]["nodeid"].endswith("test_three") and found["tests"][0]["frame"]


@needs_monitoring
def test_under_luciole_run_the_outcomes_go_to_its_trace(project, tmp_path):
    path = tmp_path / "run.sqlite"
    command = [sys.executable, "-m", "luciole", "run", "--trace", str(path), "-m", "pytest", "-p", "no:cacheprovider", "--luciole", "-k", "test_three or test_one"]
    result = subprocess.run(command, cwd=project, env=env(), capture_output=True, text=True, timeout=120)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"luciole: test outcomes recorded in {path}" in result.stdout
    assert "PytestAssertRewriteWarning" not in result.stdout
    assert not runs_of(project)
    with Trace(path) as trace:
        assert [(test["nodeid"].split("::")[1], test["outcome"]) for test in trace.tests()] == [("test_one", "passed"), ("test_three", "failed")]


def test_traces_without_tests(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    from luciole import trace

    db.executescript(trace.TABLES.replace("CREATE TABLE tests", "CREATE TABLE ignored"))
    db.executemany("INSERT INTO meta VALUES (?, ?)", [("format", trace.FORMAT), ("schema_version", "1")])
    db.execute("INSERT INTO runs (id, pid, argv, root, started_at, python, luciole, level) VALUES (1, 7, '[]', '/', 0, '3.12', '0.2.0', 1)")
    db.commit()
    db.close()
    with Trace(path) as old:
        assert old.tests() is None
    assert run_tests([path])["recorded"] == 0


class Client:
    def __init__(self, *args):
        self.process = subprocess.Popen([sys.executable, "-m", "luciole", "mcp", *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env(), text=True)
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}) + "\n")
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())["result"]

    def tool(self, name, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        return result["content"][0]["text"], result["isError"]

    def close(self):
        self.process.stdin.close()
        self.process.wait(10)


@needs_monitoring
def test_a_ci_recording_read_by_the_mcp_server(project, tmp_path):
    result = pytest_run(project, "--luciole", "--luciole-dir", "recordings")
    assert result.returncode == 1
    recordings = project / "recordings"
    [folder] = [path for path in recordings.iterdir()]
    assert f"luciole mcp --runs {recordings} --no-run" in result.stdout
    assert not runs_of(project)  # not in ~/.luciole
    client = Client("--root", str(project), "--runs", str(recordings), "--no-run")
    assert "existing recordings only" in client.request("initialize")["instructions"]
    tools = [tool["name"] for tool in client.request("tools/list")["tools"]]
    assert "run" not in tools and "tests" in tools
    text, error = client.tool("run", command="pytest")
    assert error and "--no-run" in text
    text, error = client.tool("tests")
    assert not error, text
    lines = text.splitlines()
    assert lines[0] == f"run {folder.name}"
    assert lines[1] == "5 tests: 1 failed, 1 error, 1 passed, 1 skipped, 1 xfail"
    assert lines[2].startswith("  failed  tests/test_cart.py::test_three  (call)  #")
    assert lines[3] == "          AssertionError: assert 28 == 31"
    assert lines[-1].startswith("`traceback` with exception 1 and process ")
    text, error = client.tool("tests", failed=True)
    assert "test_one" not in text and "test_db" in text
    text, error = client.tool("traceback", exception=1)
    assert not error and "expected = 31" in text and "@py_assert" not in text
    client.close()
    # One run's folder works too.
    client = Client("--root", str(project), "--runs", str(folder))
    assert "run" in [tool["name"] for tool in client.request("tools/list")["tools"]]
    assert client.tool("tests", run=folder.name)[0].startswith(f"run {folder.name}")
    client.close()
