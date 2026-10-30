"""Traces: run small programs under `luciole run --trace` and check what the file holds."""

import contextlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time

import pytest

from graphcheck import REPO
from luciole import trace, values
from luciole.cli import AUTO_START_DIR
from luciole.trace import Trace, TraceError

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")

DOC = (REPO / "docs" / "trace-format.md").read_text()


def clean_env(**extra):
    env = {key: value for key, value in os.environ.items() if not key.startswith("LUCIOLE_") or key == "LUCIOLE_HOME"}
    env["PYTHONPATH"] = str(REPO / "src")
    env.update(extra)
    return env


def write(tmp_path, files):
    for name, source in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source))


def traced(tmp_path, source, *args, level=1, env=None):
    """Run `prog.py` (or `args`) under luciole run --trace; the result and the trace file."""
    if source is not None:
        write(tmp_path, {"prog.py": source})
    path = tmp_path / "run.sqlite"
    command = [sys.executable, "-m", "luciole", "run", "--root", str(tmp_path), "--trace", str(path), "--level", str(level), *(args or ["prog.py"])]
    result = subprocess.run(command, cwd=tmp_path, env=clean_env(**(env or {})), capture_output=True, text=True, timeout=60)
    return result, path


def rows(path, query, *params):
    with contextlib.closing(sqlite3.connect(path)) as db:  # closed now: Windows cannot replace an open file
        return db.execute(query, params).fetchall()


def frames(path):
    """{id: (node, caller, line, end_kind)}"""
    return {row[0]: row[1:] for row in rows(path, "SELECT id, node, caller, line, end_kind FROM frames")}


def by_node(path, node):
    return [row for row in rows(path, "SELECT id, caller, origin, thread, end_kind, exception FROM frames WHERE node = ? ORDER BY id", node)]


# The contract


def doc_block(marker, language):
    match = re.search(rf"<!-- {marker}.*?-->\n```{language}\n(.*?)```", DOC, re.S)
    assert match, f"no {marker} block in docs/trace-format.md"
    return match.group(1)


def test_the_documented_schema_is_the_schema():
    assert doc_block("schema", "sql") == trace.SCHEMA


def test_the_documented_example(tmp_path):
    path = tmp_path / "shop.sqlite"
    command = [sys.executable, "-m", "luciole", "run", "--root", "examples", "--trace", str(path), "--level", "1", "examples/shop.py"]
    result = subprocess.run(command, cwd=REPO, env=clean_env(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    expected = json.loads(doc_block("example: shop", "json"))
    assert [list(row) for row in rows(path, "SELECT id, node, caller, line, end_kind, args FROM frames ORDER BY id")] == expected
    assert rows(path, "SELECT key, value FROM meta ORDER BY key") == [("format", "luciole-trace"), ("schema_version", "1")]
    assert rows(path, "PRAGMA user_version") == [(trace.SCHEMA_VERSION,)]
    with Trace(path) as found:
        [call] = found.calls("with_tax")["calls"]
        assert call["args"] == {"amount": {"type": "int", "repr": "15"}} and call["result"] == {"type": "float", "repr": "18.0"}
        run = found.run()
        assert run["exit_status"] == 0 and run["ended_by"] == "stop" and run["level"] == 1 and not run["truncated"]
        assert run["argv"][-1] == "examples/shop.py" and run["frames_total"] == run["frames_stored"] == 8
    # Every recorded node is a node of the graph.
    from luciole.analyzer import build_graph

    graph_ids = {node["id"] for node in build_graph(REPO / "examples" / "shop.py")["nodes"]}
    assert {row[1] for row in expected} <= graph_ids


def test_readers_refuse_other_files(tmp_path):
    (tmp_path / "other.sqlite").write_bytes(b"not a database at all, really not")
    with pytest.raises(TraceError):
        Trace(tmp_path / "other.sqlite")
    newer = tmp_path / "newer.sqlite"
    trace.create(newer).execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(TraceError, match="schema 99"):
        Trace(newer)


# The call tree


def test_nested_calls_and_recursion(tmp_path):
    result, path = traced(tmp_path, """
        def fib(n):
            return n if n < 2 else fib(n - 1) + fib(n - 2)

        def main():
            return fib(3)

        main()
    """)
    assert result.returncode == 0, result.stderr
    found = frames(path)
    assert found[1] == ("prog", None, None, "return")
    assert found[2] == ("prog:main", 1, 8, "return")
    assert found[3] == ("prog:fib", 2, 6, "return")
    # fib(3) calls fib(2) and fib(1); fib(2) calls fib(1) and fib(0).
    children = {fid: [child for child, row in found.items() if row[1] == fid] for fid in found}
    assert [found[c][0] for c in children[3]] == ["prog:fib", "prog:fib"]
    assert len(children[children[3][0]]) == 2 and children[children[3][1]] == []
    args = [json.loads(a) for (a,) in rows(path, "SELECT args FROM frames WHERE node = 'prog:fib' ORDER BY id")]
    with Trace(path) as found_trace:
        assert [found_trace.snapshot(a["n"])["repr"] for a in args] == ["3", "2", "1", "0", "1"]
        assert [found_trace.snapshot(r)["repr"] for (r,) in rows(path, "SELECT result FROM frames WHERE node = 'prog:fib' ORDER BY id")] == ["2", "1", "1", "0", "1"]
    starts = rows(path, "SELECT start_ns, end_ns FROM frames ORDER BY id")
    assert all(start <= end for start, end in starts)
    assert starts[2][0] >= starts[1][0] and starts[2][1] <= starts[1][1]  # fib runs within main


def test_a_resumed_generator_or_coroutine_is_one_frame(tmp_path):
    result, path = traced(tmp_path, """
        import asyncio, sys

        def numbers():
            yield 1
            yield 2
            return "done"

        def consume():
            total = 0
            for n in numbers():
                total += n
            return total

        def guarded():
            try:
                yield 1
            finally:
                pass

        def abandoned():
            gen = numbers()
            next(gen)
            gen.close()  # Python 3.12 closes it without running it (no handler at its yield)
            gen = guarded()
            next(gen)
            gen.close()  # runs its finally
            gen = numbers()
            next(gen)
            sys.kept = gen  # still suspended when recording stops

        async def step():
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return 5

        async def main():
            return await step()

        consume()
        abandoned()
        asyncio.run(main())
    """)
    assert result.returncode == 0, result.stderr
    [(full, consume, *_, end, _), (_, abandoned, *_, closed, _), (kept, *_, suspended, _)] = by_node(path, "prog:numbers")
    assert end == "return" and closed in ("suspended", "close") and suspended == "suspended"
    assert frames(path)[consume][0] == "prog:consume" and frames(path)[abandoned][0] == "prog:abandoned"
    (start, last_yield), = rows(path, "SELECT start_ns, end_ns FROM frames WHERE id = ?", kept)
    assert start < last_yield  # where it stopped
    assert [row[4] for row in by_node(path, "prog:guarded")] == ["close"]
    [(step, caller, *_, step_end, _)] = by_node(path, "prog:step")
    assert frames(path)[caller][0] == "prog:main" and step_end == "return"
    with Trace(path) as found:
        assert found.frame(full)["result"]["repr"] == "'done'"
        assert found.frame(step)["result"]["repr"] == "5"


def test_abandoned_generators_do_not_pile_up(tmp_path):
    result, path = traced(tmp_path, """
        import time
        from luciole import session

        def items():
            yield 1
            yield 2

        def main():
            writer = session.active.recorder.tracer.writer
            for _ in range(3):
                for _ in range(2000):
                    next(items())  # abandoned at a yield outside any try
                time.sleep(0.5)  # the writer runs meanwhile
            print("open", len(writer.open))

        main()
    """, level=0)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.split()[-1]) < 10
    # Some Pythons close them without an event (3.12.3: `suspended`), others report it (`close`).
    [(kind, count)] = rows(path, "SELECT end_kind, COUNT(*) FROM frames WHERE node = 'prog:items' GROUP BY end_kind")
    assert kind in ("suspended", "close") and count == 6000


def test_a_trace_that_cannot_be_written_stops_without_breaking_the_program(tmp_path):
    result, path = traced(tmp_path, """
        import time
        from luciole import session

        def work(n):
            return n + 1

        def main():
            tracer = session.active.recorder.tracer
            for n in range(100):
                work(n)
            with tracer.writer.lock:
                tracer.writer.db.close()  # as if the disk were full: the next write fails
            for n in range(100):
                work(n)
            time.sleep(0.5)
            for n in range(100):
                work(n)
            print("off", tracer.off, sum(len(state.buffer) for state in tracer.all_threads), len(tracer.writer.open))
            return 3

        raise SystemExit(main())
    """)
    assert result.returncode == 3, result.stderr
    assert result.stdout.split() == ["off", "True", "0", "0"]
    assert "stopped writing the trace" in result.stderr and "Traceback" not in result.stderr


def test_threads_have_their_own_roots_and_callers(tmp_path):
    result, path = traced(tmp_path, """
        import threading

        def leaf(n):
            return n

        def work(n):
            return leaf(n)

        def serve():
            workers = [threading.Thread(target=work, args=(i,), name=f"worker-{i}") for i in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

        serve()
    """)
    assert result.returncode == 0, result.stderr
    threads = dict(rows(path, "SELECT id, name FROM threads"))
    assert sorted(threads.values()) == ["MainThread", "worker-0", "worker-1"]
    [(serve, *_)] = by_node(path, "prog:serve")
    works = by_node(path, "prog:work")
    assert len(works) == 2
    for fid, caller, origin, thread, end, _ in works:
        assert caller is None and origin == serve and end == "return"  # a root, started by serve
        [(leaf, leaf_caller, _, leaf_thread, *_)] = [row for row in by_node(path, "prog:leaf") if row[1] == fid]
        assert leaf_thread == thread
    assert {threads[row[3]] for row in works} == {"worker-0", "worker-1"}


def test_asyncio_tasks_link_to_where_they_were_created(tmp_path):
    result, path = traced(tmp_path, """
        import asyncio

        async def fetch(n):
            await asyncio.sleep(0.01)
            return n

        async def fetch_all():
            return await asyncio.gather(fetch(1), fetch(2))

        def main():
            return asyncio.run(fetch_all())

        main()
    """)
    assert result.returncode == 0, result.stderr
    [(main, *_)] = by_node(path, "prog:main")
    [(fetch_all, caller, origin, *_)] = by_node(path, "prog:fetch_all")
    assert caller == main  # runs the event loop
    fetches = by_node(path, "prog:fetch")
    assert [(row[1], row[2], row[4]) for row in fetches] == [(main, fetch_all, "return")] * 2
    assert rows(path, "SELECT COUNT(*) FROM exceptions") == [(0,)]  # StopIteration is control flow


# Exceptions

EXCEPTIONS = """
    def fails(x):
        raise ValueError(f"bad {x}")

    def wraps():
        try:
            fails(1)
        except ValueError as error:
            raise RuntimeError("wrapped") from error

    def middle():
        wraps()

    def catches():
        try:
            middle()
        except RuntimeError:
            return "caught"

    def local():
        try:
            raise KeyError("k")
        except KeyError:
            pass
        try:
            int("x")  # raised in library code
        except ValueError:
            pass

    def uncaught():
        raise SystemError("boom")

    catches()
    local()
    uncaught()
"""


def test_exceptions_raised_caught_and_propagated(tmp_path):
    result, path = traced(tmp_path, EXCEPTIONS)
    assert result.returncode == 1 and "SystemError: boom" in result.stderr
    assert rows(path, "SELECT exit_status FROM runs") == [(1,)]
    with Trace(path) as found:
        exceptions = found.exceptions()["exceptions"]
    summary = [(e["type"], e["message"], e["node"], e["line"], e["handled_node"]) for e in exceptions]
    assert summary == [
        ("ValueError", "bad 1", "prog:fails", 3, "prog:wraps"),
        ("RuntimeError", "wrapped", "prog:wraps", 9, "prog:catches"),
        ("KeyError", "'k'", "prog:local", 22, "prog:local"),
        ("ValueError", "invalid literal for int() with base 10: 'x'", "prog:local", 26, "prog:local"),
        ("SystemError", "boom", "prog:uncaught", 31, None),
    ]
    assert exceptions[1]["cause"] == exceptions[0]["id"] and exceptions[1]["chain"] == "cause"
    ends = {node: (end, exception) for node, end, exception in rows(path, "SELECT node, end_kind, exception FROM frames")}
    value_error, runtime_error, *_, system_error = (e["id"] for e in exceptions)
    assert ends["prog:fails"] == ("raise", value_error)
    assert ends["prog:wraps"] == ("raise", runtime_error)
    assert ends["prog:middle"] == ("unwind", runtime_error)
    assert ends["prog:catches"] == ("return", None)
    assert ends["prog:uncaught"] == ("raise", system_error) and ends["prog"] == ("unwind", system_error)


def test_exceptions_while_handling_are_chained_as_context(tmp_path):
    result, path = traced(tmp_path, """
        def handler():
            try:
                {}["missing"]
            except KeyError:
                raise LookupError("not found")

        try:
            handler()
        except LookupError:
            pass
    """)
    assert result.returncode == 0, result.stderr
    [(key, _, _), (lookup, cause, chain)] = rows(path, "SELECT id, cause, chain FROM exceptions ORDER BY id")
    assert (cause, chain) == (key, "context")


# Failure snapshots


FAILING = """
    LIMIT = 10

    def parse(text, password):
        parts = text.split(",")
        count = len(parts)
        return int(parts[count])

    def load(name):
        lines = [name, "a,b"]
        total = 0
        for line in lines:
            total += parse(line, password="hunter2")
        return total

    def safe():
        try:
            return load("x")
        except IndexError:
            return -1

    safe()
    load("y")
"""


def test_an_exception_keeps_the_locals_of_every_frame_it_leaves(tmp_path):
    result, path = traced(tmp_path, FAILING)
    assert result.returncode == 1 and "IndexError" in result.stderr
    with Trace(path) as found:
        first = found.traceback(1)
        last = found.traceback()  # default: the last one nothing handled
    assert first["exception"]["type"] == "IndexError" and first["handled_in"]["node"] == "prog:safe"
    assert last["exception"]["id"] == 2 and last["handled_in"] is None
    parse, load, module = last["frames"]
    assert (parse["node"], parse["end"], parse["at_line"]) == ("prog:parse", "raise", 7)
    assert {name: value["repr"] for name, value in parse["locals"].items()} == {
        "text": "'y'", "password": "<redacted>", "parts": "['y']", "count": "1",
    }
    assert (load["node"], load["end"], load["at_line"]) == ("prog:load", "unwind", 13)
    assert {name: value["repr"] for name, value in load["locals"].items()} == {"name": "'y'", "lines": "['y', 'a,b']", "total": "0", "line": "'y'"}
    assert (module["node"], module["at_line"]) == ("prog", 23)
    assert {name: value["repr"] for name, value in module["locals"].items()} == {"LIMIT": "10"}  # no functions
    assert b"hunter2" not in path.read_bytes()
    text = trace.render_traceback(last)
    assert text.splitlines()[:3] == ["!2 IndexError: list index out of range", "  in prog:parse (#6), line 7: raised here", "      text = 'y'"]
    assert text.endswith("  not handled")


def test_level_0_keeps_no_locals(tmp_path):
    result, path = traced(tmp_path, FAILING, level=0)
    assert rows(path, "SELECT COUNT(*) FROM frames WHERE locals IS NOT NULL") == [(0,)]
    with Trace(path) as found:
        assert "(no locals: recorded at level 0)" in trace.render_traceback(found.traceback())


def test_locals_are_kept_for_a_limited_number_of_frames(tmp_path):
    from luciole import tracer

    result, path = traced(tmp_path, """
        def many():
            a, b, c, d, e = 1, 2, 3, 4, 5
            raise ValueError("x")

        def loop():
            for _ in range(20_000):
                try:
                    many()
                except ValueError:
                    pass

        loop()
    """)
    assert result.returncode == 0, result.stderr
    kept = rows(path, "SELECT locals FROM frames WHERE locals IS NOT NULL")
    assert len(kept) == tracer.MAX_FAILURE_FRAMES  # then no more
    assert json.loads(kept[0][0]).keys() == {"a", "b", "c", "d", "e"}


# Values


def test_level_0_has_no_values_and_level_1_has(tmp_path):
    source = """
        def add(a, b=2, *rest, scale=1, **options):
            return (a + b) * scale

        add(1, 3, 4, scale=2, mode="x")
    """
    result, path = traced(tmp_path, source, level=0)
    assert result.returncode == 0, result.stderr
    assert rows(path, "SELECT args, result FROM frames WHERE node = 'prog:add'") == [(None, None)]
    assert rows(path, "SELECT COUNT(*) FROM snapshots") == [(0,)] and rows(path, "SELECT level FROM runs") == [(0,)]
    result, path = traced(tmp_path, source, level=1)
    with Trace(path) as found:
        [call] = found.calls("prog:add")["calls"]
    reprs = {name: value["repr"] for name, value in call["args"].items()}
    assert reprs == {"a": "1", "b": "3", "rest": "(4,)", "scale": "2", "options": "{'mode': 'x'}"}
    assert call["args"]["options"]["items"] == {"len": 1, "items": [["'mode'", "'x'"]]}
    assert call["result"] == {"type": "int", "repr": "8"}


def test_values_are_taken_when_the_call_happens(tmp_path):
    result, path = traced(tmp_path, """
        def show(items):
            return len(items)

        def grow():
            items = [1]
            show(items)
            items.append(2)
            show(items)

        grow()
    """)
    with Trace(path) as found:
        assert [call["args"]["items"]["repr"] for call in found.calls("show")["calls"]] == ["[1]", "[1, 2]"]


def test_secrets_are_redacted(tmp_path):
    result, path = traced(tmp_path, """
        def login(user, password, apiKey, sort_key, keyboard, author, config):
            return "ok"

        login("ada", "hunter2", "apik-zz-123", "s", "qwerty", "me", {"token": "tok-zz-987", "region": "eu"})
    """)
    assert result.returncode == 0, result.stderr
    with Trace(path) as found:
        [call] = found.calls("login")["calls"]
    reprs = {name: value["repr"] for name, value in call["args"].items()}
    assert reprs["password"] == reprs["apiKey"] == reprs["sort_key"] == "<redacted>"
    assert reprs["user"] == "'ada'" and reprs["keyboard"] == "'qwerty'" and reprs["author"] == "'me'"
    assert reprs["config"] == "{'token': <redacted>, 'region': 'eu'}"
    content = path.read_bytes()
    assert b"hunter2" not in content and b"apik-zz-123" not in content and b"tok-zz-987" not in content


def test_secret_names():
    secrets = ["password", "db_password", "passwd", "client_secret", "authToken", "api_key", "apikey", "APIKey", "key", "sort_key", "key_file", "auth", "authorization", "credentials", "cookie", "session_id"]
    plain = ["keyboard", "monkey", "author", "authors", "name", "keys_seen", "value"]
    assert [name for name in secrets if not values.is_secret(name)] == []
    assert [name for name in plain if values.is_secret(name)] == []


def test_value_limits():
    long = values.snapshot("x" * 10_000)
    assert long[0] == "str" and len(long[1]) <= values.REPR_CHARS and long[3] is True
    many = values.snapshot(list(range(100)))
    assert many[1] == "[0, 1, 2, 3, 4, 5, 6, 7, …]" and many[3] is True
    assert json.loads(many[2]) == {"len": 100, "items": [str(i) for i in range(8)]}
    nested = values.snapshot([["y" * 500]])
    assert nested[3] is True and len(json.loads(nested[2])["items"][0]) <= values.ITEM_CHARS
    small = values.snapshot({"a": (1,)})
    assert small == ("dict", "{'a': (1,)}", '{"len": 1, "items": [["\'a\'", "(1,)"]]}', False)

    class Broken:
        def __repr__(self):
            raise RuntimeError("no")

    assert values.snapshot(Broken())[1].startswith("<Broken instance at 0x")  # reprlib catches it


def test_self_is_its_identity_without_running_its_repr(tmp_path):
    result, path = traced(tmp_path, """
        class Box:
            def __init__(self, size):
                self.size = size

            def __repr__(self):
                return f"Box({self.size})"  # fails while __init__ runs

            def grow(self):
                self.size += 1

        box = Box(1)
        box.grow()
        print(repr(box))
    """)
    assert result.returncode == 0 and "Box(2)" in result.stdout
    selves = rows(path, "SELECT json_extract(args, '$.self') FROM frames WHERE node LIKE 'prog:Box.%' ORDER BY id")
    assert len(selves) == 3 and len(set(selves)) == 1  # __init__, grow, __repr__: one object, one snapshot
    assert rows(path, "SELECT type, repr LIKE '<__main__.Box object at 0x%' FROM snapshots WHERE id = ?", selves[0][0]) == [("Box", 1)]


def test_too_many_frames_are_counted_not_stored(tmp_path):
    result, path = traced(tmp_path, """
        def tick(i):
            if i == 70:
                raise ValueError("late")
            return i

        for i in range(100):
            try:
                tick(i)
            except ValueError:
                pass
    """, env={"LUCIOLE_TRACE_MAX_FRAMES": "50"})
    assert result.returncode == 0, result.stderr
    assert rows(path, "SELECT frames_total, frames_stored, truncated FROM runs") == [(101, 50, 1)]
    assert rows(path, "SELECT COUNT(*), MAX(id) FROM frames") == [(50, 50)]
    assert rows(path, "SELECT COUNT(*) FROM exceptions") == [(0,)]  # raised in a frame not stored
    with Trace(path) as found:
        assert "truncated: 50 of 101 frames kept" in trace.render_summary(found.summary())


# Where it plugs in


def test_live_events_are_the_same_with_a_trace(tmp_path):
    write(tmp_path, {"prog.py": "def work(n):\n    return n\n\nfor i in range(3):\n    work(i)\n"})

    def calls(events_file):
        counts = {}
        for line in events_file.read_text().splitlines():
            for name, count in json.loads(line).get("calls", {}).items():
                counts[name] = counts.get(name, 0) + count
        return counts

    plain, with_trace = tmp_path / "plain.jsonl", tmp_path / "traced.jsonl"
    base = [sys.executable, "-m", "luciole", "run", "--root", str(tmp_path)]
    subprocess.run([*base, "--events", str(plain), "prog.py"], cwd=tmp_path, env=clean_env(), check=True, timeout=60)
    subprocess.run([*base, "--events", str(with_trace), "--trace", "t.sqlite", "prog.py"], cwd=tmp_path, env=clean_env(), check=True, timeout=60)
    assert calls(plain) == calls(with_trace) == {"prog": 1, "prog:work": 3}
    assert rows(tmp_path / "t.sqlite", "SELECT COUNT(*) FROM frames") == [(4,)]


def test_luciole_start_and_stop_from_code(tmp_path):
    write(tmp_path, {"prog.py": """
        import luciole

        def recorded():
            pass

        def not_recorded():
            pass

        print(luciole.start(trace="from-code.sqlite", level=1))
        recorded()
        luciole.stop()
        not_recorded()
    """})
    result = subprocess.run([sys.executable, "prog.py"], cwd=tmp_path, env=clean_env(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "True" in result.stdout, result.stderr
    assert [node for (node,) in rows(tmp_path / "from-code.sqlite", "SELECT node FROM frames")] == ["prog:recorded"]


def autostart(tmp_path, source, *args, **env):
    write(tmp_path, {"prog.py": source})
    environ = clean_env(PYTHONPATH=str(AUTO_START_DIR), LUCIOLE_ROOT=str(tmp_path), LUCIOLE_TRACE=str(tmp_path / "traces") + os.sep, **env)
    return subprocess.run([sys.executable, *(args or ["prog.py"])], cwd=tmp_path, env=environ, capture_output=True, text=True, timeout=60)


def traces(tmp_path):
    """{pid: nodes recorded} for each trace file in tmp_path/traces."""
    found = {}
    for path in sorted((tmp_path / "traces").glob("*.sqlite")):
        [(pid,)] = rows(path, "SELECT pid FROM runs")
        found[pid] = {node for (node,) in rows(path, "SELECT node FROM frames")}
    return found


def test_auto_start_writes_one_trace_per_process_without_an_engine(tmp_path):
    write(tmp_path, {"child.py": "def in_child():\n    pass\n\nin_child()\n"})
    result = autostart(tmp_path, """
        import subprocess, sys

        def in_parent():
            subprocess.run([sys.executable, "child.py"], check=True)

        in_parent()
    """)
    assert result.returncode == 0, result.stderr
    found = traces(tmp_path)
    assert len(found) == 2
    assert sorted(map(sorted, found.values())) == [["child", "child:in_child"], ["prog", "prog:in_parent"]]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork")
def test_forked_and_spawned_children_write_their_own_trace(tmp_path):
    result = autostart(tmp_path, """
        import multiprocessing, os

        def square(x):
            return x * x

        def in_child():
            pass

        if __name__ == "__main__":
            pid = os.fork()
            if pid == 0:
                in_child()
                os._exit(0)
            os.waitpid(pid, 0)
            with multiprocessing.get_context("spawn").Pool(1) as pool:
                print(sum(pool.map(square, range(5))))
    """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "30"
    found = traces(tmp_path)
    parent = next(pid for pid, nodes in found.items() if "prog" in nodes)
    assert "prog:in_child" not in found[parent] and "prog:square" not in found[parent]
    assert any(nodes == {"prog:in_child"} for pid, nodes in found.items() if pid != parent)
    assert any("prog:square" in nodes for pid, nodes in found.items() if pid != parent)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="no fork")
def test_a_forked_child_of_luciole_run_writes_next_to_the_trace(tmp_path):
    result, path = traced(tmp_path, """
        import os

        def in_child():
            pass

        pid = os.fork()
        if pid == 0:
            in_child()
            os._exit(0)
        os.waitpid(pid, 0)
    """)
    assert result.returncode == 0, result.stderr
    [child] = [p for p in tmp_path.glob("run-*.sqlite")]
    assert [node for (node,) in rows(child, "SELECT node FROM frames")] == ["prog:in_child"]
    assert "prog:in_child" not in {node for (node,) in rows(path, "SELECT node FROM frames")}


@pytest.mark.skipif(sys.platform == "win32", reason="no SIGTERM handler on Windows")
def test_sigterm_writes_the_end_of_the_trace(tmp_path):
    write(tmp_path, {"prog.py": """
        import time

        def tick(i):
            return i

        def loop():
            i = 0
            print("started", flush=True)
            while True:
                tick(i)
                i += 1
                time.sleep(0.001)

        loop()
    """})
    environ = clean_env(PYTHONPATH=str(AUTO_START_DIR), LUCIOLE_ROOT=str(tmp_path), LUCIOLE_TRACE=str(tmp_path / "traces") + os.sep)
    process = subprocess.Popen([sys.executable, "prog.py"], cwd=tmp_path, env=environ, stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline() == "started\n"
    time.sleep(0.3)
    process.terminate()
    process.wait(timeout=30)
    assert process.returncode == -signal.SIGTERM
    [path] = (tmp_path / "traces").glob("*.sqlite")
    [(ended_by, ended_at)] = rows(path, "SELECT ended_by, ended_at FROM runs")
    assert ended_by == "sigterm" and ended_at is not None
    ticks = rows(path, "SELECT COUNT(*) FROM frames WHERE node = 'prog:tick'")[0][0]
    assert ticks > 10
    assert rows(path, "SELECT end_kind FROM frames WHERE node = 'prog:loop'") == [("running",)]


# The CLI


def cli(*args):
    result = subprocess.run([sys.executable, "-m", "luciole", "trace", *map(str, args)], env=clean_env(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_cli_output(tmp_path):
    result, path = traced(tmp_path, EXCEPTIONS.replace("uncaught()\n", "") + """
    def fib(n):
        return n if n < 2 else fib(n - 1) + fib(n - 2)

    def repeat():
        for _ in range(3):
            fib(2)

    repeat()
    """)
    assert result.returncode == 0, result.stderr

    summary = cli(path, "summary")
    assert "run: " in summary and "level 1" in summary and "exit status 0" in summary
    assert re.search(r"top functions by calls:\n.*\n\s+9 .* prog:fib\n", summary)
    assert json.loads(cli(path, "summary", "--json"))["by_calls"][0]["node"] == "prog:fib"

    calls = cli(path, "calls", "fails")
    assert calls.startswith("1 calls of prog:fails\n")
    assert "fails(x=1) !! ValueError: bad 1" in calls and "from prog:wraps:7" in calls
    assert "middle !! RuntimeError: wrapped (from #" in cli(path, "calls", "middle").replace("middle()", "middle")
    limited = cli(path, "calls", "prog:fib", "--limit", "2")
    assert limited.count(" fib(n=") == 2 and "... 7 more" in limited
    assert json.loads(cli(path, "--json", "calls", "fib"))["total"] == 9

    tree = cli(path, "tree", "--depth", "3").splitlines()
    assert tree[0].startswith("prog  #1")
    assert any(re.match(r"\s{4}prog:fib ×3  #\d+", line) for line in tree)  # three identical fib(2) subtrees
    assert any(line.strip().startswith("prog:wraps") and "[raise]" in line for line in tree)
    shallow = cli(path, "tree", "--depth", "1")
    assert "calls below" in shallow and "prog:fails" not in shallow
    [root] = json.loads(cli(path, "tree", "--json", "--depth", "0"))
    assert root["node"] == "prog" and root["children"] == [] and root["more"] > 0

    exceptions = cli(path, "exceptions")
    assert exceptions.startswith("4 exceptions\n")
    assert "RuntimeError: wrapped" in exceptions and "caused by !1" in exceptions and "handled in prog:catches" in exceptions


def test_a_trace_directory_that_does_not_exist_yet_is_made(tmp_path):
    write(tmp_path, {"prog.py": "def f():\n    pass\n\nf()\n"})
    command = [sys.executable, "-m", "luciole", "run", "--root", str(tmp_path)]
    by_option = subprocess.run([*command, "--trace", "made/", "prog.py"], cwd=tmp_path, env=clean_env(), capture_output=True, text=True, timeout=60)
    by_env = subprocess.run([*command, "prog.py"], cwd=tmp_path, env=clean_env(LUCIOLE_TRACE="from-env" + os.sep), capture_output=True, text=True, timeout=60)
    assert by_option.returncode == 0 and by_env.returncode == 0, by_option.stderr + by_env.stderr
    for folder in ("made", "from-env"):
        assert (tmp_path / folder).is_dir()
        [path] = (tmp_path / folder).glob("trace-*.sqlite")
        assert ("prog:f",) in rows(path, "SELECT node FROM frames")


def test_luciole_run_says_when_the_trace_cannot_be_written(tmp_path):
    write(tmp_path, {"prog.py": "print('ran')\n", "taken": "a file, not a directory"})
    result = subprocess.run([sys.executable, "-m", "luciole", "run", "--trace", "taken/run.sqlite", "prog.py"], cwd=tmp_path, env=clean_env(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 1 and "could not write the trace" in result.stderr
    assert "Traceback" not in result.stderr and "ran" not in result.stdout


def test_cli_rejects_what_is_not_a_trace(tmp_path):
    (tmp_path / "x.sqlite").write_text("nope")
    result = subprocess.run([sys.executable, "-m", "luciole", "trace", str(tmp_path / "x.sqlite"), "summary"], env=clean_env(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 1 and "not a Luciole trace" in result.stderr


def test_env_turns_tracing_on_and_off(tmp_path, monkeypatch):
    from luciole.cli import auto_start_env

    monkeypatch.delenv("PYTHONPATH", raising=False)
    on = auto_start_env(None, None, off=False, trace=tmp_path, level=1)
    assert on["LUCIOLE_TRACE"] == str(tmp_path.resolve()) + os.sep and on["LUCIOLE_TRACE_LEVEL"] == "1"
    monkeypatch.setenv("LUCIOLE_TRACE", on["LUCIOLE_TRACE"])
    off = auto_start_env(None, None, off=True)
    assert off["LUCIOLE_TRACE"] is None and off["LUCIOLE_TRACE_LEVEL"] is None
