"""Debugging with debugpy while auto-start records: both in one process, as when VS Code debugs a
program with Luciole on. The test speaks the Debug Adapter Protocol to debugpy, as VS Code does."""

import json
import os
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from luciole.cli import AUTO_START_DIR
from test_autostart import View, calls

pytest.importorskip("debugpy")
pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")


class Adapter:
    """A minimal DAP client: requests, and waiting for events."""

    def __init__(self, port):
        self.connection = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.stream = self.connection.makefile("rb")
        self.seq = 0
        self.events = []

    def request(self, command, **arguments):
        self.seq += 1
        body = json.dumps({"seq": self.seq, "type": "request", "command": command, "arguments": arguments}).encode()
        self.connection.sendall(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        return self.seq

    def receive(self):
        length = 0
        while (line := self.stream.readline().strip()) != b"":
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        return json.loads(self.stream.read(length))

    def wait(self, check):
        while True:
            message = self.receive()
            if check(message):
                return message


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_breakpoints_stop_while_the_graph_lights_up(tmp_path):
    (tmp_path / "prog.py").write_text(textwrap.dedent("""\
        def total(prices):
            result = sum(prices)
            return result


        def main():
            return total([1, 2, 3])


        main()
    """))
    view = View()
    port = free_port()
    environ = {key: value for key, value in os.environ.items() if not key.startswith("LUCIOLE_") or key == "LUCIOLE_HOME"}
    environ |= {"PYTHONPATH": str(AUTO_START_DIR), "LUCIOLE_ROOT": str(tmp_path)}
    command = [sys.executable, "-m", "debugpy", "--listen", f"127.0.0.1:{port}", "--wait-for-client", "prog.py"]
    process = subprocess.Popen(command, cwd=tmp_path, env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        adapter = None
        for _ in range(100):
            try:
                adapter = Adapter(port)
                break
            except OSError:
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read().decode())
                time.sleep(0.1)
        adapter.request("initialize", adapterID="debugpy", clientID="test", pathFormat="path", linesStartAt1=True, columnsStartAt1=True)
        adapter.request("attach", justMyCode=True)
        adapter.wait(lambda m: m.get("event") == "initialized")
        adapter.request("setBreakpoints", source={"path": str(tmp_path / "prog.py")}, breakpoints=[{"line": 2}])
        adapter.request("configurationDone")
        stopped = adapter.wait(lambda m: m.get("event") == "stopped")
        assert stopped["body"]["reason"] == "breakpoint"
        trace = adapter.request("stackTrace", threadId=stopped["body"]["threadId"], startFrame=0, levels=1)
        frame = adapter.wait(lambda m: m.get("type") == "response" and m.get("request_seq") == trace)["body"]["stackFrames"][0]
        assert (frame["name"], frame["line"]) == ("total", 2)  # what the brain is told when the debugger stops
        adapter.request("continue", threadId=stopped["body"]["threadId"])
        adapter.wait(lambda m: m.get("event") == "terminated")
        adapter.request("disconnect")
        adapter.connection.close()  # as VS Code does: debugpy's adapter process then exits too
        process.wait(timeout=30)
    finally:
        process.kill()
    assert any(calls(p["messages"]) == {"prog": 1, "prog:main": 1, "prog:total": 1} for p in view.processes())
