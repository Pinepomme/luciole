"""The recorded history as VS Code browses it: run folders (`luciole runs`), the call tree a level
at a time (`children`), replay frames (`frames`) and the query server (`serve`)."""

import json
import subprocess
import sys
import textwrap

import pytest

from graphcheck import REPO
from luciole import runs
from luciole.browse import query, replay_frames, stacks_at
from luciole.trace import Trace

needs_monitoring = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs Python 3.12")

PROGRAM = """\
import threading


def leaf(n):
    return n * 2


def middle(n):
    total = 0
    for i in range(n):
        total += leaf(i)
    return total


def fails(x):
    raise ValueError(f"bad {x}")


def worker():
    leaf(100)


def main():
    middle(5)
    leaf(1)
    leaf(2)
    try:
        fails(3)
    except ValueError:
        pass
    t = threading.Thread(target=worker, name="helper")
    t.start()
    t.join()


main()
"""


def luciole(*args, cwd=None):
    result = subprocess.run([sys.executable, "-m", "luciole", *map(str, args)], cwd=cwd, env=env(), capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def env():
    import os

    return {**{k: v for k, v in os.environ.items() if not k.startswith("LUCIOLE_") or k == "LUCIOLE_HOME"}, "PYTHONPATH": str(REPO / "src")}


@pytest.fixture
def recorded(tmp_path):
    """A run of PROGRAM recorded the way Run Live records it: a run folder, a level 1 trace in it."""
    (tmp_path / "prog.py").write_text(textwrap.dedent(PROGRAM))
    made = json.loads(luciole("runs", "new", "--root", tmp_path, "--command", json.dumps(["python", "prog.py"]), "--json"))
    folder = made["folder"]
    luciole("run", "--root", tmp_path, "--trace", folder + "/", "--level", 1, tmp_path / "prog.py", cwd=tmp_path)
    luciole("runs", "finish", folder, "--exit-status", 0)
    (trace,) = runs.processes_of(runs.Path(folder))
    return tmp_path, made, trace


@needs_monitoring
def test_runs_lists_vscode_and_agent_runs_alike(recorded):
    root, made, trace = recorded
    # The MCP server's runs land in the same folder (runs_folder of the same root).
    agent_id, agent_folder = runs.new_run(root.resolve(), ["pytest", "-x"], root, 1)
    listing = json.loads(luciole("runs", "--root", root, "--json"))
    assert listing["folder"] == str(runs.runs_folder(root.resolve()))
    assert [run["id"] for run in listing["runs"]] == sorted([made["id"], agent_id], reverse=True)
    mine = next(run for run in listing["runs"] if run["id"] == made["id"])
    assert mine["by"] == "vscode" and mine["command"] == ["python", "prog.py"] and mine["exit_status"] == 0
    assert mine["state"] == "finished"
    (process,) = mine["processes"]
    assert process["file"] == str(trace) and process["frames"] >= 8 and process["exceptions"] == 1 and process["uncaught"] == 0
    agent = next(run for run in listing["runs"] if run["id"] == agent_id)
    assert agent["by"] == "agent" and agent["state"] == "running" and agent["processes"] == []
    runs.finish_run(agent_folder, 1)
    agent = next(run for run in runs.list_runs(root)["runs"] if run["id"] == agent_id)
    assert agent["state"] == "empty"  # ended, nothing recorded (Python older than 3.12, say)
    assert "by vscode" in luciole("runs", "--root", root)


def test_runs_keeps_the_last_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "KEEP_RUNS", 3)
    made = [runs.new_run(tmp_path, ["python", f"{n}.py"], tmp_path, 1)[1] for n in range(5)]
    assert [path.exists() for path in made] == [False, False, True, True, True]


@needs_monitoring
def test_children_page_and_collapse(recorded):
    _, _, path = recorded
    with Trace(path) as trace:
        roots = trace.children()
        assert [call["node"] for call in roots["calls"]] == ["prog", "prog:worker"] and roots["next"] is None
        module = roots["calls"][0]
        assert module["calls"] == 1 and module["count"] == 1  # main
        main = trace.children(module["frame"])["calls"][0]
        assert main["node"] == "prog:main" and main["caller"]["line"] == 36
        below = trace.children(main["frame"])["calls"]
        assert [(call["node"].split(":")[1], call["count"]) for call in below] == [("middle", 1), ("leaf", 2), ("fails", 1)]
        middle, leaves, fails = below
        assert middle["calls"] == 5 and middle["args"]["n"]["repr"] == "5" and middle["result"]["repr"] == "20"
        assert leaves["last"] > leaves["frame"] and leaves["args"]["n"]["repr"] == "1"
        assert fails["end"] == "raise" and fails["exception"]["type"] == "ValueError"
        # A collapsed item opens on its calls, one by one.
        each = trace.children(main["frame"], after=leaves["frame"] - 1, until=leaves["last"], collapse=False)["calls"]
        assert [call["args"]["n"]["repr"] for call in each] == ["1", "2"]
        # Pages: `next` is where the next one starts.
        first = trace.children(middle["frame"], limit=2, collapse=False)
        assert len(first["calls"]) == 2 and first["next"] == first["calls"][-1]["frame"]
        rest = trace.children(middle["frame"], after=first["next"], collapse=False)
        assert len(rest["calls"]) == 3 and rest["next"] is None
        collapsed = trace.children(middle["frame"])["calls"]
        assert len(collapsed) == 1 and collapsed[0]["count"] == 5 and collapsed[0]["calls"] == 0
        # The thread's frame is a root of its own, started by main.
        worker = roots["calls"][1]
        assert worker["thread"] == "helper" and worker["origin"] == main["frame"]


@needs_monitoring
def test_replay_frames_are_live_frames(recorded):
    _, _, path = recorded
    with Trace(path) as trace:
        run = trace.run()
        leaf = trace.calls("leaf", limit=1)["calls"][0]  # the first leaf call, from middle
        at = leaf["start_ns"] + 1
        stacks = stacks_at(trace, at)
        assert stacks == [{"thread": "MainThread", "stack": ["prog", "prog:main", "prog:middle", "prog:leaf"]}][: len(stacks)]
        assert stacks[0]["stack"][:3] == ["prog", "prog:main", "prog:middle"]
        frames = replay_frames(trace, 0, run["duration_ns"], max(run["duration_ns"] // 10, 1))
        assert 9 <= len(frames) <= 11
        assert all(set(frame) == {"t", "stacks", "calls", "links", "spawns"} for frame in frames)
        # Every call is counted once, in the frame of its start; links and spawns as live mode sends them.
        calls, links, spawns = {}, {}, {}
        for frame in frames:
            for key, total in (("calls", calls), ("links", links), ("spawns", spawns)):
                for name, count in frame[key].items():
                    total[name] = total.get(name, 0) + count
        assert calls == {"prog": 1, "prog:main": 1, "prog:middle": 1, "prog:leaf": 8, "prog:fails": 1, "prog:worker": 1}
        assert links["prog:middle>prog:leaf"] == 5 and links["prog:main>prog:leaf"] == 2 and links["prog:worker>prog:leaf"] == 1
        assert spawns == {"prog:main>prog:worker": 1}
        assert "prog:main>prog:worker" not in links
        # Jumping: the frame at a moment needs nothing before it.
        (one,) = replay_frames(trace, at - 1, at + 1, 2)
        assert one["stacks"] == stacks and one["calls"] == {"prog:leaf": 1}
        assert replay_frames(trace, run["duration_ns"] + 10, run["duration_ns"] + 20, 5)[0]["stacks"] == []


@needs_monitoring
def test_serve_answers_queries_on_stdin(recorded):
    _, _, path = recorded
    requests = [
        {"id": 1, "query": "children", "args": {}},
        {"id": 2, "query": "frames", "args": {"from": 0, "to": 1000, "every": 1000}},
        {"id": 3, "query": "nope"},
        {"id": 4, "query": "frame", "args": {"frame": 2}},
        {"id": 5, "query": "traceback", "args": {}},
    ]
    process = subprocess.run(
        [sys.executable, "-m", "luciole", "trace", str(path), "serve"],
        input="\n".join(json.dumps(r) for r in requests) + "\n", env=env(), capture_output=True, text=True, timeout=60,
    )
    replies = [json.loads(line) for line in process.stdout.splitlines()]
    assert [reply["id"] for reply in replies] == [1, 2, 3, 4, 5]
    assert replies[0]["result"]["calls"][0]["node"] == "prog"
    assert len(replies[1]["result"]) == 1
    assert "unknown query" in replies[2]["error"]
    assert replies[3]["result"]["node"] == "prog:main"
    traceback = replies[4]["result"]
    assert traceback["exception"]["type"] == "ValueError" and traceback["frames"][0]["locals"]["x"]["repr"] == "3"


@needs_monitoring
def test_cli_queries(recorded):
    _, _, path = recorded
    assert "#1 prog" in luciole("trace", path, "children")
    assert "fails(x=3)" in luciole("trace", path, "frame", 7) or "fails" in luciole("trace", path, "children", "--frame", 2)
    frames = json.loads(luciole("trace", path, "frames", "--every", 1000, "--json"))
    assert frames and frames[0]["calls"]["prog"] == 1
    assert "started: prog ×1" in luciole("trace", path, "frames", "--every", 1000)


@needs_monitoring
def test_origin_queries_for_the_editor(recorded):
    """What VS Code's "Where did this value come from?" asks: the function at a line, its last
    calls, then the origin, with the files of the steps' functions; the graph is built once."""
    root, _, path = recorded
    graphs = {}
    with Trace(path) as trace:
        assert query(trace, "function_at", {"file": str(root / "prog.py"), "line": 11}, graphs) == {"node": "prog:middle", "kind": "function", "line": 8}
        assert query(trace, "function_at", {"file": "prog.py", "line": 36}, graphs)["node"] == "prog"  # module code
        assert query(trace, "function_at", {"file": str(root.parent / "elsewhere.py"), "line": 1}, graphs) is None
        assert len(graphs) == 1
        calls = query(trace, "last_calls", {"node": "prog:leaf", "limit": 2})
        assert calls["total"] == 8 and [c["args"]["n"]["repr"] for c in calls["calls"]] == ["100", "2"]  # newest first
        first = trace.calls("prog:leaf", limit=1)["calls"][0]["frame"]
        found = query(trace, "origin", {"node": "prog:leaf", "variable": "n", "frame": first}, graphs)
        steps = found["steps"]
        assert steps["kind"] == "argument" and steps["value"] == "0" and steps["caller"]["node"] == "prog:middle"
        assert steps["children"][0]["node"] == "prog:middle" and steps["children"][0]["children"][0]["value"] == "5"
        assert found["files"] == {"prog:leaf": "prog.py", "prog:main": "prog.py", "prog:middle": "prog.py"} and found["root"] == str(root)
        assert found["text"].startswith("where n in prog:leaf")
        several = query(trace, "origin", {"node": "middle", "variable": "total"}, graphs)["steps"]
        # Assigned on lines 9 and 11: middle returned 20 and line 9 assigns 0, so line 11 made it.
        assert several["kind"] == "write" and several["line"] == 11 and several["value"] == "20" and "inferred" in several["note"]
        with pytest.raises(ValueError, match="no calls of nope"):
            query(trace, "origin", {"node": "nope", "variable": "x"}, graphs)


@needs_monitoring
def test_serve_answers_origin_and_runs_new_takes_a_scope(recorded):
    root, _, path = recorded
    requests = [{"id": 1, "query": "origin", "args": {"node": "leaf", "variable": "return"}}, {"id": 2, "query": "origin", "args": {"node": "nope", "variable": "x"}}]
    process = subprocess.run(
        [sys.executable, "-m", "luciole", "trace", str(path), "serve"],
        input="\n".join(json.dumps(r) for r in requests) + "\n", env=env(), capture_output=True, text=True, timeout=60,
    )
    replies = [json.loads(line) for line in process.stdout.splitlines()]
    assert replies[0]["result"]["steps"]["kind"] == "returned_by" and replies[0]["result"]["steps"]["value"] == "200"
    assert "no calls of nope" in replies[1]["error"]
    made = json.loads(luciole("runs", "new", "--root", root, "--command", json.dumps(["python", "prog.py"]), "--level", 2, "--scope", "prog:middle,leaf", "--json"))
    meta = json.loads((runs.Path(made["folder"]) / "command.json").read_text())
    assert meta["level"] == 2 and meta["scope"] == ["prog:middle", "leaf"]
