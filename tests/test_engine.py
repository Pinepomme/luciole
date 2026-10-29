"""The engine: handshake, routing between recorders and viewers, discovery, idle exit."""

import json
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from graphcheck import REPO
from luciole import protocol
from luciole.engine import Engine, serve


@pytest.fixture
def engine():
    engine = Engine(idle=0)
    thread = threading.Thread(target=engine.serve, daemon=True)
    thread.start()
    yield engine
    engine.stop()
    thread.join(timeout=5)


class Client:
    """A recorder or viewer speaking the protocol by hand."""

    def __init__(self, port, role, **fields):
        self.connection = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.lines = self.connection.makefile(encoding="utf-8")
        protocol.send(self.connection, protocol.hello(role, **fields) if role else fields)

    def send(self, message):
        protocol.send(self.connection, message)

    def receive(self):
        line = self.lines.readline()
        return json.loads(line) if line else None

    def receive_until(self, check):
        while (message := self.receive()) is not None:
            if check(message):
                return message
        return None

    def close(self):
        self.lines.close()
        self.connection.close()


def test_the_handshake_checks_the_protocol(engine):
    viewer = Client(engine.port, "viewer")
    assert viewer.receive() == {"welcome": {"protocol": protocol.PROTOCOL, "version": protocol.__version__}}
    old = Client(engine.port, None, hello={"role": "viewer", "protocol": protocol.PROTOCOL + 1})
    assert "refused" in old.receive() and old.receive() is None  # then closed
    rude = Client(engine.port, None, cmd="pause")
    assert rude.receive() == {"refused": "expected a hello"}


def test_routes_between_recorders_and_the_viewers_of_their_project(engine, tmp_path):
    app, other = tmp_path / "app", tmp_path / "other"
    app_viewer = Client(engine.port, "viewer", roots=[str(app)])
    every_viewer = Client(engine.port, "viewer")
    for viewer in (app_viewer, every_viewer):
        viewer.receive()  # welcome
    recorder = Client(engine.port, "recorder", pid=11, argv=["python", "app.py"], root=str(app / "src"))
    stranger = Client(engine.port, "recorder", pid=12, argv=["python", "other.py"], root=str(other))
    assert recorder.receive()["welcome"] and stranger.receive()["welcome"]

    announced = app_viewer.receive()["process"]
    assert announced["pid"] == 11 and announced["root"] == str(app / "src")
    assert {every_viewer.receive()["process"]["pid"], every_viewer.receive()["process"]["pid"]} == {11, 12}

    stranger.send({"t": 0.1, "calls": {"other:main": 1}})
    recorder.send({"t": 0.1, "calls": {"app:main": 1}})
    assert app_viewer.receive() == {"from": announced["id"], "t": 0.1, "calls": {"app:main": 1}}  # not the stranger's

    app_viewer.send({"to": announced["id"], "cmd": "step", "thread": 7, "mode": "over"})
    assert recorder.receive() == {"cmd": "step", "thread": 7, "mode": "over"}
    app_viewer.send({"cmd": "pause"})  # to every process the viewer shows
    assert recorder.receive() == {"cmd": "pause"}

    recorder.close()
    assert app_viewer.receive_until(lambda m: "exit" in m) == {"exit": announced["id"]}
    # A viewer that joins later hears of the processes already connected.
    late = Client(engine.port, "viewer", roots=[str(other)])
    late.receive()
    assert late.receive()["process"]["pid"] == 12

    for viewer in (app_viewer, every_viewer, late):
        viewer.close()
    assert stranger.receive_until(lambda m: "cmd" in m) == {"cmd": "resume"}  # nobody left to resume it


def test_registers_and_exits_when_idle():
    engine = Engine(idle=0.3)
    thread = threading.Thread(target=engine.serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while protocol.engine_port() != engine.port and time.monotonic() < deadline:
        time.sleep(0.01)
    assert json.loads(protocol.engine_file().read_text())["pid"] == os.getpid()
    thread.join(timeout=10)
    assert not thread.is_alive() and not protocol.engine_file().exists()


def test_a_second_engine_leaves_the_running_one(engine):
    while protocol.engine_port() != engine.port:
        time.sleep(0.01)
    assert serve() == 0  # returns at once
    assert protocol.engine_port() == engine.port


def test_engines_started_together_leave_one_running():
    engines = [Engine(idle=0) for _ in range(4)]
    barrier = threading.Barrier(len(engines))

    def serve_together(engine):
        barrier.wait()
        engine.serve()

    threads = [threading.Thread(target=serve_together, args=(engine,), daemon=True) for engine in engines]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 10
    while sum(thread.is_alive() for thread in threads) > 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    [running] = [engine for engine, thread in zip(engines, threads) if thread.is_alive()]
    assert protocol.engine_port() == running.port
    viewer = Client(running.port, "viewer")
    assert "welcome" in viewer.receive()
    viewer.close()
    running.stop()
    for thread in threads:
        thread.join(timeout=5)
    assert not protocol.engine_file().exists()


def test_luciole_engine_leaves_a_running_engine(engine):
    while protocol.engine_port() != engine.port:
        time.sleep(0.01)
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run([sys.executable, "-m", "luciole", "engine", "--idle", "1"], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "already runs" in result.stderr
    assert protocol.engine_port() == engine.port


@pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")
def test_luciole_run_reports_to_the_running_engine(engine, tmp_path):
    while protocol.engine_port() != engine.port:
        time.sleep(0.01)
    viewer = Client(engine.port, "viewer")
    viewer.receive()
    (tmp_path / "prog.py").write_text("def work():\n    pass\n\nwork()\n")
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run([sys.executable, "-m", "luciole", "run", "--root", str(tmp_path), "prog.py"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert viewer.receive_until(lambda m: "prog:work" in m.get("calls", {}))
    viewer.close()


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_roots_reached_through_a_symlink_match(engine, tmp_path):
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks here")
    viewer = Client(engine.port, "viewer", roots=[str(link)])  # the workspace, opened through the link
    viewer.receive()
    recorder = Client(engine.port, "recorder", pid=11, argv=[], root=str(real / "src"))  # resolved
    recorder.receive()
    assert viewer.receive()["process"]["pid"] == 11
    other = Client(engine.port, "viewer", roots=[str(real / "missing")])  # need not exist
    other.receive()
    linked = Client(engine.port, "recorder", pid=12, argv=[], root=str(link / "missing" / "deeper"))
    linked.receive()
    assert other.receive()["process"]["pid"] == 12
    for client in (viewer, recorder, other, linked):
        client.close()


def test_a_recorder_cut_off_mid_line_still_exits_cleanly(engine):
    viewer = Client(engine.port, "viewer")
    viewer.receive()
    recorder = Client(engine.port, "recorder", pid=11, argv=[])
    recorder.receive()
    number = viewer.receive()["process"]["id"]
    recorder.send({"t": 0.1, "calls": {"app:main": 1}})
    recorder.connection.sendall(b'{"t":0.2,"calls":{"app:ma')  # killed mid-line
    recorder.close()
    assert viewer.receive() == {"from": number, "t": 0.1, "calls": {"app:main": 1}}
    assert viewer.receive() == {"exit": number}  # on a line of its own
    viewer.close()


def test_programs_resume_when_no_viewer_shows_them_any_more(engine, tmp_path):
    app_viewer = Client(engine.port, "viewer", roots=[str(tmp_path / "app")])
    other_viewer = Client(engine.port, "viewer", roots=[str(tmp_path / "other")])
    app = Client(engine.port, "recorder", pid=11, argv=[], root=str(tmp_path / "app"))
    other = Client(engine.port, "recorder", pid=12, argv=[], root=str(tmp_path / "other"))
    for client in (app_viewer, other_viewer, app, other):
        assert "welcome" in client.receive()
    assert other_viewer.receive()["process"]["pid"] == 12
    app_viewer.close()
    assert app.receive() == {"cmd": "resume"}  # nobody left to resume it
    other_viewer.send({"cmd": "pause"})
    assert other.receive() == {"cmd": "pause"}  # still shown: left paused
    for client in (other_viewer, app, other):
        client.close()
