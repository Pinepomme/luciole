"""Auto-start: any Python process with the auto-start folder on PYTHONPATH lights up the view."""

import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from graphcheck import REPO
from luciole import protocol
from luciole.cli import AUTO_START_DIR, auto_start_env, env_commands
from luciole.engine import Engine

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")


class View:
    """A real engine, and a viewer connected to it that keeps each process's messages."""

    def __init__(self, roots=()):
        self.engine = Engine(idle=0)
        self.port = self.engine.port
        threading.Thread(target=self.engine.serve, daemon=True).start()
        deadline = time.monotonic() + 10
        while protocol.engine_port() != self.port and time.monotonic() < deadline:
            time.sleep(0.01)
        self.connection = socket.create_connection(("127.0.0.1", self.port))
        protocol.send(self.connection, protocol.hello("viewer", name="test", roots=[str(root) for root in roots]))
        self.welcome = None
        self.infos: dict[int, dict] = {}
        self.messages: dict[int, list[dict]] = {}
        self.exited: set[int] = set()
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        with self.connection.makefile(encoding="utf-8") as lines:
            for line in lines:
                message = json.loads(line)
                if "welcome" in message:
                    self.welcome = message["welcome"]
                elif "process" in message:
                    self.infos[message["process"]["id"]] = message["process"]
                    self.messages.setdefault(message["process"]["id"], [])
                elif "exit" in message:
                    self.exited.add(message["exit"])
                else:
                    self.messages.setdefault(message.pop("from"), []).append(message)

    def processes(self):
        """Info and messages of each process that reported calls, once every process left."""
        deadline = time.monotonic() + 20
        while set(self.infos) - self.exited and time.monotonic() < deadline:
            time.sleep(0.02)
        self.engine.stop()
        self.connection.close()
        return [{"info": self.infos[id], "messages": messages} for id, messages in self.messages.items() if calls(messages)]


def calls(messages):
    counts = {}
    for message in messages:
        for name, count in message.get("calls", {}).items():
            counts[name] = counts.get(name, 0) + count
    return counts


def python(tmp_path, source, *args, port=None, extra_path=(), **env):
    (tmp_path / "prog.py").write_text(textwrap.dedent(source))
    environ = {key: value for key, value in os.environ.items() if not key.startswith("LUCIOLE_") or key == "LUCIOLE_HOME"}
    environ["PYTHONPATH"] = os.pathsep.join([str(AUTO_START_DIR), *map(str, extra_path)])
    environ["LUCIOLE_ROOT"] = str(tmp_path)
    if port:
        environ["LUCIOLE_LIVE_PORT"] = str(port)
    environ.update(env)
    return subprocess.run([sys.executable, *args], cwd=tmp_path, env=environ, capture_output=True, text=True, timeout=60)


PROGRAM = """
    import sys

    def work():
        return 42

    work()
    recording = "luciole" in map(sys.monitoring.get_tool, range(6))
    print("recorder loaded:", recording or any(name.endswith("recorder") for name in sys.modules))
"""


def test_off_without_a_port(tmp_path):
    result = python(tmp_path, PROGRAM, "prog.py")
    assert result.returncode == 0 and result.stderr == ""
    assert "recorder loaded: False" in result.stdout


def test_nothing_starts_without_an_engine(tmp_path):
    result = python(tmp_path, PROGRAM, "prog.py")
    assert result.returncode == 0 and result.stderr == ""
    assert "recorder loaded: False" in result.stdout


def test_nothing_starts_when_nothing_listens(tmp_path):
    with socket.create_server(("127.0.0.1", 0)) as server:
        closed_port = server.getsockname()[1]
    result = python(tmp_path, PROGRAM, "prog.py", port=closed_port)
    assert result.returncode == 0 and result.stderr == ""
    assert "recorder loaded: False" in result.stdout


def test_reports_to_the_view(tmp_path):
    view = View()
    result = python(tmp_path, PROGRAM, "prog.py", port=view.port)
    assert result.returncode == 0 and result.stderr == ""
    [process] = view.processes()
    assert view.welcome["protocol"] == protocol.PROTOCOL
    assert process["info"]["pid"] > 0 and process["info"]["argv"][-1] == "prog.py"
    assert process["info"]["root"] == str(tmp_path.resolve())
    assert calls(process["messages"]) == {"prog": 1, "prog:work": 1}
    assert process["messages"][-1] == {"done": True}


def test_finds_the_engine_without_a_port(tmp_path):
    view = View()
    result = python(tmp_path, PROGRAM, "prog.py")  # LUCIOLE_HOME holds the engine's file
    assert result.returncode == 0 and "recorder loaded: True" in result.stdout
    assert [calls(process["messages"]) for process in view.processes()] == [{"prog": 1, "prog:work": 1}]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork")
def test_forked_children_report_as_their_own_process(tmp_path):
    view = View()
    result = python(tmp_path, """
        import os

        def in_parent():
            pass

        def in_child():
            pass

        pid = os.fork()
        if pid == 0:
            in_child()
            os._exit(0)
        os.waitpid(pid, 0)
        in_parent()
    """, "prog.py", port=view.port)
    assert result.returncode == 0, result.stderr
    processes = view.processes()
    assert len(processes) == 2
    assert len({process["info"]["pid"] for process in processes}) == 2
    by_function = {name: process["info"]["pid"] for process in processes for name in calls(process["messages"])}
    assert by_function["prog:in_child"] != by_function["prog:in_parent"]


def test_child_processes_inherit_it(tmp_path):
    view = View()
    (tmp_path / "child.py").write_text("def in_child():\n    pass\n\nin_child()\n")
    result = python(tmp_path, """
        import subprocess, sys

        subprocess.run([sys.executable, "child.py"], check=True)
    """, "prog.py", port=view.port)
    assert result.returncode == 0, result.stderr
    assert any("child:in_child" in calls(process["messages"]) for process in view.processes())


def test_the_hidden_sitecustomize_still_runs(tmp_path):
    own = tmp_path / "own"
    own.mkdir()
    (own / "sitecustomize.py").write_text("import builtins\nbuiltins.OWN_SITECUSTOMIZE = True\n")
    view = View()
    result = python(tmp_path, "print('own:', OWN_SITECUSTOMIZE)\n", "prog.py", port=view.port, extra_path=[own])
    assert result.returncode == 0, result.stderr
    assert "own: True" in result.stdout


def test_an_empty_pythonpath_does_not_add_the_working_directory(tmp_path):
    # VS Code's switch prepends "<folder><separator>": on an empty PYTHONPATH, Python would read
    # the trailing empty entry as the working directory.
    script = "import os, sys; print(os.getcwd() in sys.path, os.environ['PYTHONPATH'])"
    result = python(tmp_path, "", "-c", script, PYTHONPATH=str(AUTO_START_DIR) + os.pathsep)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", str(AUTO_START_DIR)]


def test_profilers_keep_working(tmp_path):
    view = View()
    result = python(tmp_path, PROGRAM, "-m", "cProfile", "prog.py", port=view.port)
    assert result.returncode == 0, result.stderr
    assert "function calls" in result.stdout
    assert any("prog:work" in calls(process["messages"]) for process in view.processes())


def test_luciole_run_takes_over(tmp_path):
    view = View()
    env = {"PYTHONPATH": os.pathsep.join([str(AUTO_START_DIR), str(REPO / "src")])}
    result = python(tmp_path, PROGRAM, "-m", "luciole", "run", "--root", str(tmp_path), "--port", str(view.port), "prog.py", port=view.port, **env)
    assert result.returncode == 0, result.stderr
    [process] = view.processes()  # auto-start leaves luciole run to its own recorder
    assert calls(process["messages"]) == {"prog": 1, "prog:work": 1}


def test_start_and_stop_from_code(tmp_path):
    events = tmp_path / "events.jsonl"
    result = python(tmp_path, f"""
        import luciole

        def recorded():
            pass

        def not_recorded():
            pass

        print("started:", luciole.start(events={str(events)!r}))
        recorded()
        luciole.stop()
        not_recorded()
        print("without a view:", luciole.start(port=1))
    """, "prog.py", PYTHONPATH=str(REPO / "src"))
    assert result.returncode == 0, result.stderr
    assert "started: True" in result.stdout and "without a view: False" in result.stdout
    messages = [json.loads(line) for line in events.read_text().splitlines()]
    assert "prog:recorded" in calls(messages) and "prog:not_recorded" not in calls(messages)
    assert messages[-1] == {"done": True}


def test_env_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    assert auto_start_env(None, None, off=False)["LUCIOLE_LIVE_PORT"] is None  # found when programs start
    on = auto_start_env(5000, tmp_path, off=False)
    assert on["PYTHONPATH"] == os.pathsep.join([str(AUTO_START_DIR), "/somewhere"])
    assert on["LUCIOLE_LIVE_PORT"] == "5000" and on["LUCIOLE_ROOT"] == str(tmp_path.resolve())
    monkeypatch.setenv("PYTHONPATH", on["PYTHONPATH"])
    off = auto_start_env(None, None, off=True)
    assert off == {"PYTHONPATH": "/somewhere", "LUCIOLE_LIVE_PORT": None, "LUCIOLE_ROOT": None}
    assert env_commands(off, "sh") == "export PYTHONPATH=/somewhere\nunset LUCIOLE_LIVE_PORT\nunset LUCIOLE_ROOT"
    assert env_commands({"A": "it's"}, "powershell") == "$env:A = 'it''s'"


OWN_LUCIOLE = """
    import luciole

    def work():
        return 42

    work()
    print("luciole:", luciole.WHO)
"""


def own_luciole(tmp_path):
    (tmp_path / "luciole").mkdir()
    (tmp_path / "luciole" / "__init__.py").write_text("WHO = 'own'\n")


def test_a_program_imports_its_own_luciole_without_an_engine(tmp_path):
    own_luciole(tmp_path)
    result = python(tmp_path, OWN_LUCIOLE, "prog.py")
    assert result.returncode == 0, result.stderr
    assert "luciole: own" in result.stdout


def test_a_program_imports_its_own_luciole_with_an_engine(tmp_path):
    own_luciole(tmp_path)
    view = View()
    result = python(tmp_path, OWN_LUCIOLE, "prog.py")
    assert result.returncode == 0, result.stderr
    assert "luciole: own" in result.stdout
    [process] = view.processes()  # the recorder still reports
    assert calls(process["messages"])["prog:work"] == 1


def test_luciole_start_leaves_auto_start_reporting(tmp_path):
    view = View()
    result = python(tmp_path, """
        import luciole

        def work():
            pass

        print("started:", luciole.start())
        work()
    """, "prog.py", extra_path=[REPO / "src"])
    assert result.returncode == 0, result.stderr
    assert "started: True" in result.stdout
    [process] = view.processes()  # one recorder, not one per copy of luciole
    assert calls(process["messages"])["prog:work"] == 1
