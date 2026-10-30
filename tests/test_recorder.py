"""Live recorder: run small programs under `luciole run` and check the events."""

import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from graphcheck import REPO
from luciole import protocol
from luciole.analyzer import build_graph

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")


def welcomed(server):
    """Accept a recorder and answer its hello as the engine does: its connection and lines."""
    connection, _ = server.accept()
    lines = connection.makefile(encoding="utf-8")
    assert "hello" in json.loads(lines.readline())
    connection.sendall(b'{"welcome": {"protocol": %d}}\n' % protocol.PROTOCOL)
    return connection, lines


def luciole_run(tmp_path, *args):
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    env.pop("LUCIOLE_LIVE_PORT", None)
    return subprocess.run([sys.executable, "-m", "luciole", "run", *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)


def record(tmp_path, files, *args):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))
    events = tmp_path / "events.jsonl"
    result = luciole_run(tmp_path, "--root", str(tmp_path), "--events", str(events), *args)
    frames = [json.loads(line) for line in events.read_text().splitlines()]
    return result, frames


def totals(frames, key):
    counts = {}
    for frame in frames:
        for name, count in frame.get(key, {}).items():
            counts[name] = counts.get(name, 0) + count
    return counts


def test_counts_calls_and_links_with_graph_ids(tmp_path):
    result, frames = record(tmp_path, {}, str(REPO / "examples" / "shop.py"))
    # shop.py lives outside tmp_path: nothing is reported, but the program still runs.
    assert result.returncode == 0 and "Total: 18.0" in result.stdout
    assert frames[-1] == {"done": True}
    assert totals(frames, "calls") == {}

    result = luciole_run(REPO, "--root", str(REPO / "examples"), "--events", str(tmp_path / "shop.jsonl"), "examples/shop.py")
    frames = [json.loads(line) for line in (tmp_path / "shop.jsonl").read_text().splitlines()]
    calls = totals(frames, "calls")
    assert calls["shop:Cart.add"] == 2 and calls["shop:with_tax"] == 1 and calls["shop"] == 1
    assert totals(frames, "links")["shop:main>shop:Cart.add"] == 2
    graph_ids = {n["id"] for n in build_graph(REPO / "examples" / "shop.py")["nodes"]}
    assert {name for name in calls if name != "shop:Cart"} <= graph_ids  # class bodies run too


def test_samples_the_running_stack(tmp_path):
    _, frames = record(tmp_path, {"slow.py": """
        import time

        def outer():
            inner()

        def inner():
            time.sleep(0.3)

        outer()
    """}, "slow.py")
    assert ("MainThread", ["slow", "slow:outer", "slow:inner"]) in stacks(frames)


def stacks(frames):
    return [(s["thread"], s["stack"]) for frame in frames for s in frame.get("stacks", [])]


def test_threads_have_their_own_named_stack_and_a_spawn_link(tmp_path):
    _, frames = record(tmp_path, {"threads.py": """
        import threading, time

        def work():
            time.sleep(0.3)

        def serve():
            workers = [threading.Thread(target=work, name=f"worker-{i}") for i in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

        serve()
    """}, "threads.py")
    seen = stacks(frames)
    assert ("MainThread", ["threads", "threads:serve"]) in seen
    assert ("worker-0", ["threads:work"]) in seen and ("worker-1", ["threads:work"]) in seen
    assert totals(frames, "spawns") == {"threads:serve>threads:work": 2}
    assert "threads:serve>threads:work" not in totals(frames, "links")


ASYNC_APP = """
    import asyncio

    async def fetch(n):
        await asyncio.sleep(0.3)
        return n

    async def fetch_all():
        return await asyncio.gather(fetch(1), fetch(2))

    def schedule():
        return asyncio.ensure_future(fetch(9))

    async def main():
        pending = schedule()
        await fetch_all()
        await pending

    asyncio.run(main())
"""


def test_async_tasks_wait_and_link_to_where_they_were_created(tmp_path):
    _, frames = record(tmp_path, {"app.py": ASYNC_APP}, "app.py")
    assert {"app:main": 1, "app:fetch_all": 1, "app:fetch": 3} in [frame.get("waiting") for frame in frames]
    assert totals(frames, "spawns") == {"app>app:main": 1, "app:schedule>app:fetch": 1, "app:fetch_all>app:fetch": 2}
    # Real calls stay links; the event loop's runner is not the tasks' caller.
    assert totals(frames, "links") == {"app:main>app:schedule": 1, "app:main>app:fetch_all": 1}
    assert frames[-2].get("waiting") == {}  # nothing left waiting at the end


def test_awaited_coroutine_is_a_call_and_cancellation_stops_waiting(tmp_path):
    _, frames = record(tmp_path, {"cancel.py": """
        import asyncio

        async def step():
            await asyncio.sleep(0.2)

        async def forever():
            await step()
            await asyncio.sleep(10)

        async def main():
            task = asyncio.ensure_future(forever())
            await asyncio.sleep(0.4)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(main())
    """}, "cancel.py")
    assert totals(frames, "links")["cancel:forever>cancel:step"] == 1
    assert {"cancel:main": 1, "cancel:forever": 1} in [frame.get("waiting") for frame in frames]
    assert frames[-2].get("waiting") == {}


def test_generators_leave_the_stack_when_they_yield(tmp_path):
    _, frames = record(tmp_path, {"gen.py": """
        import time

        def numbers():
            yield 1
            yield 2

        def consume():
            for _ in numbers():
                time.sleep(0.2)

        consume()
    """}, "gen.py")
    seen = stacks(frames)
    assert ("MainThread", ["gen", "gen:consume"]) in seen
    assert not [stack for _, stack in seen if "gen:numbers" in stack]


def test_nested_functions_modules_and_callers_through_libraries(tmp_path):
    _, frames = record(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/tool.py": """
            def apply(values):
                def double(x):
                    return x * 2
                return list(map(double, values))
        """,
        "main.py": """
            from pkg.tool import apply
            apply([1, 2, 3])
        """,
    }, "main.py")
    calls = totals(frames, "calls")
    assert calls["pkg.tool:apply.double"] == 3
    # `map` is a builtin: the caller is still found, as the nearest project frame.
    assert totals(frames, "links")["pkg.tool:apply>pkg.tool:apply.double"] == 3


def test_callbacks_from_deep_library_stacks_keep_the_stack(tmp_path):
    # Like torch tracing a model: the library calls project code back from far below the caller.
    _, frames = record(tmp_path, {
        ".lib/deep.py": """
            def descend(depth, callback):
                return callback() if depth == 0 else descend(depth - 1, callback)
        """,
        "main.py": """
            import sys, time
            sys.path.insert(0, ".lib")
            from deep import descend

            def forward():
                time.sleep(0.3)

            def export():
                descend(100, forward)
                time.sleep(0.3)

            export()
        """,
    }, "main.py")
    stacks = [s["stack"] for frame in frames for s in frame.get("stacks", [])]
    assert ["main", "main:export", "main:forward"] in stacks
    assert ["main", "main:export"] in stacks  # still running after the callback returned
    assert totals(frames, "links")["main:export>main:forward"] == 1


def test_exit_code_and_module_mode(tmp_path):
    result, frames = record(tmp_path, {"app/__init__.py": "", "app/__main__.py": "import sys\nsys.exit(3)\n"}, "-m", "app")
    assert result.returncode == 3
    assert totals(frames, "calls") == {"app": 1, "app.__main__": 1}


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGINT to a child process on Windows")
def test_ctrl_c_stops_the_program_and_its_traceback_ends_in_project_code(tmp_path):
    """What Stop in the view does: SIGINT, then the program unwinds as it would without Luciole."""
    (tmp_path / "busy.py").write_text(textwrap.dedent("""\
        def step(i):
            return i * 2

        try:
            total = 0
            print("started", flush=True)
            while True:
                total += step(total % 7)
        finally:
            print("cleanup ran", flush=True)
    """))
    events = tmp_path / "events.jsonl"
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    env.pop("LUCIOLE_LIVE_PORT", None)
    command = [sys.executable, "-m", "luciole", "run", "--root", str(tmp_path), "--events", str(events), "busy.py"]
    process = subprocess.Popen(command, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert process.stdout.readline() == "started\n"
    process.send_signal(signal.SIGINT)
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode != 0 and "cleanup ran" in stdout
    traceback = stderr.strip().splitlines()
    assert traceback[-1] == "KeyboardInterrupt"
    assert "busy.py" in [line for line in traceback if line.lstrip().startswith("File ")][-1]
    assert "recorder.py" not in stderr.split("busy.py", 1)[1]
    assert json.loads(events.read_text().splitlines()[-1]) == {"done": True}


def test_streams_to_a_socket(tmp_path):
    (tmp_path / "hello.py").write_text("def hi():\n    pass\nhi()\n")
    server = socket.create_server(("127.0.0.1", 0))
    received = []

    def accept():
        connection, lines = welcomed(server)
        with connection, lines:
            received.extend(json.loads(line) for line in lines)

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    result = luciole_run(tmp_path, "--root", str(tmp_path), "--port", str(server.getsockname()[1]), "hello.py")
    thread.join(timeout=10)
    server.close()
    assert result.returncode == 0
    assert totals(received, "calls") == {"hello": 1, "hello:hi": 1}
    assert received[-1] == {"done": True}


def test_the_hello_comes_before_the_first_frame(tmp_path):
    # The engine refuses a connection whose first line is not a hello: the pump thread, which
    # sends frames, must not run before it is written.
    (tmp_path / "prog.py").write_text(textwrap.dedent("""
        import socket, threading
        from luciole import protocol, recorder

        pumped = threading.Event()

        def pump(self):  # the first tick at once
            self.recorder.flush()
            pumped.set()
            while not self.stopping.wait(recorder.FRAME):
                self.recorder.flush()

        def slow_hello(*args, hello=protocol.hello, **kwargs):
            pumped.wait(0.5)  # a pump started before the hello sends its frame first
            return hello(*args, **kwargs)

        recorder.Session.pump = pump
        protocol.hello = slow_hello
        server = socket.create_server(("127.0.0.1", 0))
        connection = socket.create_connection(server.getsockname())
        engine, _ = server.accept()
        engine.sendall(b'{"welcome": {"protocol": %d}}\\n' % protocol.PROTOCOL)  # start() waits for it
        recorder.start(root=".", connection=connection)
        recorder.stop()
        print(engine.makefile().readline())
    """))
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    env.pop("LUCIOLE_LIVE_PORT", None)
    result = subprocess.run([sys.executable, "prog.py"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["hello"]["role"] == "recorder"


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGTERM handler on Windows")
def test_a_process_ending_at_once_still_delivers_its_calls(tmp_path):
    # A process that ends before reading the engine's welcome closes a socket holding unread
    # data: the connection is reset, and the engine loses the calls it had not read yet. Pool
    # workers terminated right after their last task did (macOS CI). No control thread here,
    # so only start() can read the welcome.
    (tmp_path / "prog.py").write_text(textwrap.dedent("""
        import os, signal, sys, time
        from luciole import recorder

        recorder.Session.control = lambda self: None

        def work():
            pass

        recorder.start(root=".", port=int(sys.argv[1]))
        time.sleep(0.2)  # the welcome has arrived
        work()
        os.kill(os.getpid(), signal.SIGTERM)  # as a pool terminates its workers
    """))
    server = socket.create_server(("127.0.0.1", 0))
    received = []

    def engine():
        connection, _ = server.accept()
        with connection:
            hello = b""
            while not hello.endswith(b"\n"):  # byte by byte: what follows stays in the socket
                hello += connection.recv(1)
            connection.sendall(b'{"welcome": {"protocol": %d}}\n' % protocol.PROTOCOL)
            time.sleep(0.5)  # the process is gone by now
            try:
                with connection.makefile(encoding="utf-8") as lines:
                    received.extend(json.loads(line) for line in lines)
            except OSError as error:
                received.append({"error": repr(error)})

    thread = threading.Thread(target=engine, daemon=True)
    thread.start()
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    env.pop("LUCIOLE_LIVE_PORT", None)
    result = subprocess.run([sys.executable, "prog.py", str(server.getsockname()[1])], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    thread.join(timeout=10)
    server.close()
    assert result.returncode == -signal.SIGTERM, result.stderr
    assert totals(received, "calls") == {"prog:work": 1} and received[-1] == {"done": True}, received


def test_an_engine_slow_to_answer_does_not_hold_the_program(tmp_path):
    # A port that something else holds may never answer the hello: the program waits a moment
    # at most, and the control thread still reads the answer when it comes.
    (tmp_path / "prog.py").write_text(textwrap.dedent("""
        import sys, time
        from luciole import recorder

        def work():
            pass

        began = time.monotonic()
        recorder.start(root=".", port=int(sys.argv[1]))
        print(time.monotonic() - began)
        work()
        time.sleep(1.5)  # the late welcome arrives
    """))
    server = socket.create_server(("127.0.0.1", 0))
    received = []

    def engine():
        connection, _ = server.accept()
        with connection, connection.makefile(encoding="utf-8") as lines:
            assert "hello" in json.loads(lines.readline())
            time.sleep(1)  # past the recorder's wait
            connection.sendall(b'{"welcome": {"protocol": %d}}\n' % protocol.PROTOCOL)
            received.extend(json.loads(line) for line in lines)

    thread = threading.Thread(target=engine, daemon=True)
    thread.start()
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    env.pop("LUCIOLE_LIVE_PORT", None)
    result = subprocess.run([sys.executable, "prog.py", str(server.getsockname()[1])], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    thread.join(timeout=10)
    server.close()
    assert result.returncode == 0, result.stderr
    assert float(result.stdout) < 1, result.stdout
    assert totals(received, "calls") == {"prog:work": 1} and received[-1] == {"done": True}, received


PAUSABLE_APP = """\
    import time

    LIMIT = 3


    class Cart:
        def __init__(self):
            self.items = {"apple": 2}
            self.total = 0.5


    def tick(cart, step):
        scaled = step * 2
        cart.total += scaled
        return scaled


    cart = Cart()
    for step in range(400):
        tick(cart, step)
        time.sleep(0.002)
"""


def test_pause_shows_variables_and_expands_objects(tmp_path):
    (tmp_path / "app.py").write_text(textwrap.dedent(PAUSABLE_APP))
    server = socket.create_server(("127.0.0.1", 0))
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "LUCIOLE_LIVE_PORT": str(server.getsockname()[1])}
    command = [sys.executable, "-m", "luciole", "run", "--root", str(tmp_path), "app.py"]
    process = subprocess.Popen(command, cwd=tmp_path, env=env)
    try:
        connection, lines = welcomed(server)

        def send(command):
            connection.sendall((json.dumps(command) + "\n").encode())

        def next_message(key):
            for line in lines:
                message = json.loads(line)
                if key in message:
                    return message[key]
            raise AssertionError(f"no {key!r} message")

        while "app:tick" not in next_message("calls"):
            pass  # wait for the loop: before it, LIMIT and cart may not exist yet
        send({"cmd": "pause"})
        paused = next_message("paused")
        assert paused["thread"] == "MainThread"
        names = [frame["node"] for frame in paused["frames"]]
        assert names[-1] == "app" and set(names) <= {"app", "app:tick"}
        module = paused["frames"][-1]
        top = {item["name"]: item for item in module["vars"]}
        assert top["LIMIT"] == {"name": "LIMIT", "type": "int", "value": "3"}
        assert "time" not in top and "Cart" not in top  # modules and classes are left out
        assert top["cart"]["type"] == "Cart" and "ref" in top["cart"]

        send({"cmd": "expand", "ref": top["cart"]["ref"]})
        children = next_message("children")
        assert children["ref"] == top["cart"]["ref"]
        attributes = {item["name"]: item for item in children["vars"]}
        assert set(attributes) == {"items", "total"}
        send({"cmd": "expand", "ref": attributes["items"]["ref"]})
        assert next_message("children")["vars"] == [{"name": "'apple'", "type": "int", "value": "2"}]

        send({"cmd": "resume"})
        assert next_message("done") is True
        assert process.wait(timeout=30) == 0
    finally:
        process.kill()
        server.close()


STEPPING_APP = """\
    def helper(x):
        y = x + 1
        return y


    def main():
        a = 1
        b = helper(a)
        c = b * 2
        return c


    for _ in range(300000):
        main()
"""


def test_step_over_stays_in_the_frame_or_returns_to_the_caller(tmp_path):
    (tmp_path / "app.py").write_text(textwrap.dedent(STEPPING_APP))
    server = socket.create_server(("127.0.0.1", 0))
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "LUCIOLE_LIVE_PORT": str(server.getsockname()[1])}
    command = [sys.executable, "-m", "luciole", "run", "--root", str(tmp_path), "app.py"]
    process = subprocess.Popen(command, cwd=tmp_path, env=env)
    try:
        connection, lines = welcomed(server)

        def send(command):
            connection.sendall((json.dumps(command) + "\n").encode())

        def next_paused():
            for line in lines:
                message = json.loads(line)
                if "paused" in message:
                    return message["paused"]
            raise AssertionError("never paused")

        for _ in range(100):  # until it stops inside a function, not on the module's loop
            send({"cmd": "pause"})
            paused = next_paused()
            if len(paused["frames"]) > 1:
                break
            send({"cmd": "resume"})
        stops = [[(frame["node"], frame["line"]) for frame in paused["frames"]]]
        for _ in range(8):
            send({"cmd": "step", "thread": paused["id"]})
            paused = next_paused()
            stops.append([(frame["node"], frame["line"]) for frame in paused["frames"]])

        for before, after in zip(stops, stops[1:]):
            # Never into a callee: same frame on a later line, or back in a caller.
            assert len(after) <= len(before), (before, after)
            if len(after) == len(before) and before[0][0] == "app:main" and before[0][1] == 8:
                assert after[0] == ("app:main", 9)  # b = helper(a) stepped over
        assert ("app:main", 9) in [stack[0] for stack in stops]  # went through main
        locals_at_9 = [stack for stack in stops if stack[0] == ("app:main", 9)]
        assert locals_at_9 and all(len(stack) == 2 for stack in locals_at_9)

        send({"cmd": "resume"})
        assert process.wait(timeout=60) == 0
    finally:
        process.kill()
        server.close()


def test_step_into_enters_calls_and_step_out_returns_to_the_caller(tmp_path):
    (tmp_path / "app.py").write_text(textwrap.dedent(STEPPING_APP))
    server = socket.create_server(("127.0.0.1", 0))
    env = {**os.environ, "PYTHONPATH": str(REPO / "src"), "LUCIOLE_LIVE_PORT": str(server.getsockname()[1])}
    command = [sys.executable, "-m", "luciole", "run", "--root", str(tmp_path), "app.py"]
    process = subprocess.Popen(command, cwd=tmp_path, env=env)
    try:
        connection, lines = welcomed(server)

        def send(command):
            connection.sendall((json.dumps(command) + "\n").encode())

        def where(paused):
            return [(frame["node"], frame["line"]) for frame in paused["frames"]][:2]

        def next_paused():
            for line in lines:
                message = json.loads(line)
                if "paused" in message:
                    return message["paused"]
            raise AssertionError("never paused")

        def step(paused, mode):
            send({"cmd": "step", "thread": paused["id"], "mode": mode})
            return next_paused()

        for _ in range(100):  # until it stops in main, before the call to helper
            send({"cmd": "pause"})
            paused = next_paused()
            if where(paused)[0] == ("app:main", 7):
                break
            send({"cmd": "resume"})
        assert where(paused)[0] == ("app:main", 7)
        paused = step(paused, "into")  # a = 1: no call, next line
        assert where(paused)[0] == ("app:main", 8)
        paused = step(paused, "into")  # b = helper(a): into helper
        assert where(paused) == [("app:helper", 2), ("app:main", 8)]
        paused = step(paused, "out")  # back in main, after the call
        assert where(paused)[0] == ("app:main", 9)
        assert {item["name"]: item["value"] for item in paused["frames"][0]["vars"]}["b"] == "2"

        send({"cmd": "resume"})
        assert process.wait(timeout=60) == 0
    finally:
        process.kill()
        server.close()
