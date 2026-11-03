"""Fault localization from a recorded test run (docs/fault-localization.md).

    suspects(paths, graph)            functions ranked by how much failing tests ran them and
                                      passing tests did not (spectrum-based, Ochiai)
    compare(trace_a, frame_a, trace_b, frame_b)   the first point where two executions differ
    compare_tests(paths_a, paths_b)   the same, for a failing test and the closest passing one

`paths` are the traces of one run (one per process: pytest-xdist workers, subprocesses). Tests
come from the `tests` table the pytest plugin fills; without it, from the test functions'
frames (in test files, named test*), failed when they ended by an exception. Every function
returns plain dicts (JSON-ready); localize_text.py renders them.
"""

from __future__ import annotations

import difflib
import json
import math
import re
from pathlib import Path

from luciole.trace import FAILED, Trace

TEST_PACKAGES = ("test", "tests", "testing")
SKIP_TYPES = ("Skip", "XFail", "xfail")  # unittest.SkipTest, pytest's Skipped and XFailed
EXAMPLE_RANK = {"same_arguments": 0, "like_passing": 2}  # call_example() kinds, in ties; others 1
MAX_ALIGN = 1_000_000  # children pairs an alignment compares (n × m) before falling back to order
ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")


def is_test_node(node: str) -> bool:
    """Whether a function is test code: in a test file (test_*.py, *_test.py, conftest.py) or a
    test package (tests/), by its module's name."""
    module = node.split(":", 1)[0]
    parts = module.split(".")
    last = parts[-1]
    return last.startswith("test_") or last.endswith("_test") or last in ("conftest", *TEST_PACKAGES) or any(part in TEST_PACKAGES for part in parts[:-1])


def is_test_function(node: str) -> bool:
    if ":" not in node or not is_test_node(node):
        return False
    return node.rsplit(":", 1)[1].rsplit(".", 1)[-1].startswith("test")


class Recording:
    """The traces of one run, opened once."""

    def __init__(self, paths: list[Path]):
        self.paths = [Path(path) for path in paths]
        self.traces: dict[Path, Trace] = {}
        self.pids: dict[Path, int] = {}

    def trace(self, path: Path) -> Trace:
        if path not in self.traces:
            self.traces[path] = Trace(path)
            self.pids[path] = self.traces[path].run()["pid"]
        return self.traces[path]

    def pid(self, path: Path) -> int:
        self.trace(path)
        return self.pids[path]

    def close(self) -> None:
        for trace in self.traces.values():
            trace.close()

    def __enter__(self) -> Recording:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# Tests


def test_runs(recording: Recording) -> tuple[list[dict], str]:
    """The tests that passed or failed, each `{"path", "pid", "nodeid", "node", "frame",
    "outcome", "start_ns", "end_ns"}` (outcome `passed` or `failed`), and where outcomes came
    from: `recorded` (the plugin's `tests` table) or `inferred` (test functions' frames)."""
    found, recorded = [], False
    for path in recording.paths:
        trace = recording.trace(path)
        tests = trace.tests()
        if tests is None:
            continue
        recorded = recorded or bool(tests)
        for test in tests:
            outcome = "failed" if test["outcome"] in FAILED else "passed" if test["outcome"] in ("passed", "xpass") else None
            if outcome is None:
                continue  # skipped, xfail: an expected failure says nothing about the code
            nodeid = test["nodeid"] + (f" (attempt {test['attempt']})" if test["attempt"] > 1 else "")
            found.append({"path": path, "pid": recording.pid(path), "nodeid": nodeid, "node": test["node"], "frame": test["frame"], "outcome": outcome, "start_ns": test["start_ns"], "end_ns": test["end_ns"]})
    if recorded:
        return found, "recorded"
    for path in recording.paths:
        found += inferred_tests(recording, path)
    return found, "inferred"


def inferred_tests(recording: Recording, path: Path) -> list[dict]:
    """Test functions' calls (not called by another test function): failed when they ended by an
    exception other than a skip."""
    trace = recording.trace(path)
    end = trace.run()["duration_ns"]
    rows = trace.db.execute(
        """
        SELECT f.id, f.node, f.start_ns, f.end_ns, f.end_kind, e.type, c.node AS caller FROM frames f
        LEFT JOIN exceptions e ON e.id = f.exception LEFT JOIN frames c ON c.id = f.caller
        WHERE f.node LIKE '%test%' ORDER BY f.id
        """
    ).fetchall()
    found, seen = [], {}
    for frame, node, start, stop, kind, error, caller in rows:
        if not is_test_function(node) or (caller and is_test_function(caller)):
            continue
        if kind == "return":
            outcome = "passed"
        elif kind in ("raise", "unwind") and not any(word in (error or "") for word in SKIP_TYPES):
            outcome = "failed"
        else:
            continue
        seen[node] = seen.get(node, 0) + 1
        found.append({"path": path, "pid": recording.pid(path), "nodeid": node, "node": node, "frame": frame, "outcome": outcome, "start_ns": start, "end_ns": stop if stop is not None else end})
    for test in found:  # a parametrized test function runs several times: name each call
        if seen[test["node"]] > 1:
            test["nodeid"] = f"{test['node']} #{test['frame']}"
    return found


def executed(recording: Recording, test: dict) -> set[str]:
    """The project functions (not test code) that ran while a test ran."""
    if "executed" not in test:
        rows = recording.trace(test["path"]).db.execute("SELECT DISTINCT node FROM frames WHERE start_ns BETWEEN ? AND ?", (test["start_ns"], test["end_ns"]))
        test["executed"] = {node for (node,) in rows if ":" in node and not is_test_node(node) and node != test["node"]}
    return test["executed"]


def inputs(recording: Recording, test: dict, cap: int = 200) -> set[tuple]:
    """(function, arguments) of the first project calls a test made, addresses left out: tells
    apart tests that ran the same functions on other inputs."""
    if "inputs" not in test:
        trace = recording.trace(test["path"])
        rows = trace.db.execute("SELECT id, node FROM frames WHERE start_ns BETWEEN ? AND ? ORDER BY id LIMIT ?", (test["start_ns"], test["end_ns"], cap))
        test["inputs"] = {(node, arguments(trace.frame(fid))) for fid, node in rows.fetchall() if ":" in node and not is_test_node(node) and node != test["node"]}
    return test["inputs"]


# Suspects


def suspects(paths: list[Path], graph: dict | None = None, top: int = 10, hints: int = 3) -> dict:
    """Functions ranked by Ochiai: failed(f) / sqrt(total_failed × (failed(f) + passed(f))), where
    failed(f) and passed(f) count the failing and passing tests that ran f. `{"source", "failed",
    "passed", "processes", "method", "suspects", "functions", "ties"}`; each suspect has
    `node`, `score`, `failed`, `passed`, `callers` (distinct calling functions), `file`, `line`
    (from `graph`, the static graph), the `rank` it shares with ties, and for the first `hints`,
    `example`: a call made in a failing test that no passing test made (see call_example());
    within a tie, functions whose example shows such a call come first.
    `method` is `ochiai`, `no_passing` (ranked by failing tests that ran it, then fewest
    callers) or `no_failing` (nothing ranked)."""
    with Recording(paths) as recording:
        tests, source = test_runs(recording)
        failing = [test for test in tests if test["outcome"] == "failed"]
        passing = [test for test in tests if test["outcome"] == "passed"]
        found = {"source": source, "failed": len(failing), "passed": len(passing), "processes": len(recording.paths), "suspects": [], "functions": 0, "ties": 0}
        found["failing_tests"] = [test_label(test) for test in failing[:5]]
        if not failing:
            found["method"] = "no_failing" if tests else "no_tests"
            return found
        found["method"] = "ochiai" if passing else "no_passing"
        failed_by: dict[str, int] = {}
        passed_by: dict[str, int] = {}
        for group, counts in ((failing, failed_by), (passing, passed_by)):
            for test in group:
                for node in executed(recording, test):
                    counts[node] = counts.get(node, 0) + 1
        found["functions"] = len(failed_by)
        ranked = []
        for node, failed in failed_by.items():
            passed = passed_by.get(node, 0)
            ranked.append({"node": node, "score": failed / math.sqrt(len(failing) * (failed + passed)), "failed": failed, "passed": passed})
        candidates = sorted(ranked, key=lambda item: (-item["score"], -item["failed"], item["passed"]))[: max(top * 3, 30)]
        for item in candidates:
            item["callers"] = callers_of(recording, item["node"])
        if passing:
            order = lambda item: (-item["score"], -item["failed"], item["passed"], item["callers"], item["node"])  # noqa: E731
        else:
            order = lambda item: (-item["failed"], item["callers"], item["node"])  # noqa: E731
        candidates.sort(key=order)
        key = (lambda item: (round(item["score"], 9), item["failed"], item["passed"])) if passing else (lambda item: (item["failed"], item["callers"]))
        best = key(candidates[0])
        found["ties"] = sum(1 for item in (ranked if passing else candidates) if key(item) == best)
        # Values break ties: within the same score, a function called in a failing test as no
        # passing test called it comes before one whose calls were all made by passing tests too.
        for item in candidates[: max(hints, min(found["ties"], top))]:
            item["example"] = call_example(recording, item["node"], failing, passing)
        hint = lambda item: EXAMPLE_RANK.get((item.get("example") or {}).get("kind"), 1)  # noqa: E731
        candidates.sort(key=lambda item: (*order(item)[:-2], hint(item), *order(item)[-2:]))
        rank, previous = 0, None
        for index, item in enumerate(candidates[:top]):
            if key(item) != previous:
                rank, previous = index + 1, key(item)
            item["rank"] = rank
        places = places_of(graph)
        for index, item in enumerate(candidates[:top]):
            item["file"], item["line"] = places.get(item["node"], (None, None))
            if index >= hints:
                item.pop("example", None)
            elif "example" not in item:
                item["example"] = call_example(recording, item["node"], failing, passing)
        for item in candidates[top:]:
            item.pop("example", None)
        lines_of(recording, candidates[:top], failing, passing)
        found["suspects"] = candidates[:top]
        return found


def test_label(test: dict) -> dict:
    return {"nodeid": test["nodeid"], "frame": test["frame"], "pid": test["pid"]}


def callers_of(recording: Recording, node: str) -> int:
    """Distinct functions that called `node`, over the run's processes (no project caller counts as one)."""
    callers = set()
    for path in recording.paths:
        rows = recording.trace(path).db.execute("SELECT DISTINCT c.node FROM frames f LEFT JOIN frames c ON c.id = f.caller WHERE f.node = ?", (node,))
        callers.update(row[0] for row in rows)
    return len(callers)


def places_of(graph: dict | None) -> dict[str, tuple]:
    if not graph:
        return {}
    return {node["id"]: (node.get("file"), (node.get("loc") or {}).get("line")) for node in graph["nodes"] if node["kind"] == "function"}


def call_example(recording: Recording, node: str, failing: list[dict], passing: list[dict], cap: int = 100) -> dict | None:
    """A call of `node` in a failing test that no passing test made, with the closest passing
    call: `{"kind", "failing", "passing"}`, each `{"test", "pid", "call"}` (a frame as
    Trace.frame() gives it). `kind`: `same_arguments` (a passing test called it with the same
    arguments and got another result), `different` (the closest passing call, by arguments equal),
    `like_passing` (every call in failing tests was also made in a passing one: `failing` is one
    of them), `no_passing_calls`. None at level 0 (no values)."""

    def calls(tests: list[dict]) -> list[tuple[dict, dict]]:
        out = []
        for test in tests:
            trace = recording.trace(test["path"])
            rows = trace.db.execute("SELECT id FROM frames WHERE node = ? AND start_ns BETWEEN ? AND ? ORDER BY id LIMIT 20", (node, test["start_ns"], test["end_ns"]))
            out += [(test, trace.frame(row[0])) for row in rows.fetchall()]
            if len(out) >= cap:
                break
        return out

    failed, passed = calls(failing), calls(passing)
    if not failed or "args" not in failed[0][1]:
        return None
    side = lambda pair: {"test": pair[0]["nodeid"], "pid": pair[0]["pid"], "call": pair[1]}  # noqa: E731
    if not passed:
        return {"kind": "no_passing_calls", "failing": side(failed[0]), "passing": None}
    seen = {signature(call) for _, call in passed}
    by_args = {}
    for pair in passed:
        by_args.setdefault(arguments(pair[1]), pair)
    new = [pair for pair in failed if signature(pair[1]) not in seen]
    for pair in new:
        same = by_args.get(arguments(pair[1]))
        if same is not None:
            return {"kind": "same_arguments", "failing": side(pair), "passing": side(same)}
    if not new:
        return {"kind": "like_passing", "failing": side(failed[0]), "passing": None}
    pair = new[0]
    mine = dict(arguments(pair[1]))
    closest = max(passed, key=lambda other: sum(1 for name, value in arguments(other[1]) if mine.get(name) == value))
    return {"kind": "different", "failing": side(pair), "passing": side(closest)}


def lines_of(recording: Recording, items: list[dict], failing: list[dict], passing: list[dict]) -> None:
    """Level 2: the lines of `items` that assigned a variable, ranked by Ochiai as functions are,
    from the `writes` table, when the run's scope covered them: `item["lines"]`, best first."""
    nodes = [item["node"] for item in items]
    if not nodes:
        return
    counts: dict[tuple, list[int]] = {}
    marks = ",".join("?" * len(nodes))
    for index, group in enumerate((failing, passing)):
        for test in group:
            trace = recording.trace(test["path"])
            if not trace._has_table("writes"):
                continue
            rows = trace.db.execute(
                f"SELECT DISTINCT f.node, w.line FROM writes w JOIN frames f ON f.id = w.frame WHERE w.time_ns BETWEEN ? AND ? AND f.node IN ({marks})",
                (test["start_ns"], test["end_ns"], *nodes),
            )
            for node, line in rows:
                counts.setdefault((node, line), [0, 0])[index] += 1
    for item in items:
        lines = []
        for (node, line), (failed, passed) in counts.items():
            if node == item["node"] and failed:
                lines.append({"line": line, "score": failed / math.sqrt(len(failing) * (failed + passed)), "failed": failed, "passed": passed})
        if lines:
            item["lines"] = sorted(lines, key=lambda found: (-found["score"], found["line"]))[:5]


def normal(value: dict | None) -> str | None:
    """A value's repr without memory addresses, which differ between runs."""
    return None if value is None else ADDRESS.sub(" at 0x…", value["repr"])


def arguments(call: dict) -> tuple:
    return tuple((name, normal(value)) for name, value in (call.get("args") or {}).items())


def ending(call: dict) -> tuple:
    if call["end"] == "return":
        return ("return", normal(call.get("result")))
    if call["end"] in ("raise", "unwind"):
        return ("raise", (call["exception"] or {}).get("type"))
    return (call["end"],)


def signature(call: dict) -> tuple:
    return arguments(call), ending(call)


# Compare


class Side:
    """One execution being compared: a trace and the frames read from it."""

    def __init__(self, trace: Trace):
        self.trace = trace
        self.has_args = True

    def children(self, frame: int, depth: int = 0) -> list[dict]:
        """The calls `frame` made; calls of test code (a test's helpers, assertion functions)
        stand for the calls they made: what is compared is the project code the tests ran."""
        rows = self.trace.db.execute("SELECT id, node, args, result, end_kind, exception FROM frames WHERE caller = ? ORDER BY id", (frame,))
        found = []
        for row in map(dict, rows.fetchall()):
            if is_test_node(row["node"]) and depth < 20:
                found += self.children(row["id"], depth + 1)
            else:
                found.append(row)
        return found

    def args(self, row: dict) -> dict | None:
        if row["args"] is None:
            return None
        return {name: normal(self.trace.snapshot(value)) for name, value in json.loads(row["args"]).items()}

    def ending(self, row: dict) -> tuple:
        if row["end_kind"] == "return":
            return ("return", normal(self.trace.snapshot(row["result"])) if row["args"] is not None else None)
        if row["end_kind"] in ("raise", "unwind"):
            found = self.trace.db.execute("SELECT type FROM exceptions WHERE id = ?", (row["exception"],)).fetchone() if row["exception"] else None
            return ("raise", found[0] if found else None)
        return (row["end_kind"],)


def compare(trace_a: Trace, frame_a: int, trace_b: Trace, frame_b: int, limit: int = 20_000) -> dict:
    """Walk two calls' trees side by side, children in order, matched by function (an alignment
    of each level's sequence of functions, difflib's), and find the first difference in execution
    order: a call made on one side only (`call_path`), a call that raised on one side only
    (`raised`), or that returned another value (`result`). Arguments that differ are not a
    divergence on their own (inputs differ: a parametrized test), but the first call whose
    arguments differ is reported (`first_arguments`), and `path` says, for each call from the
    roots to the divergence, which arguments differ. `{"a", "b", "divergence", "first_arguments",
    "compared", "cut"}`; `a` and `b` are the roots (frames as Trace.frame() gives them);
    `divergence` is None when nothing differs (within `limit` pairs compared). Calls of test
    code below the roots are transparent: their calls are compared in their place."""
    row_a = trace_a.db.execute("SELECT id, node, args, result, end_kind, exception FROM frames WHERE id = ?", (frame_a,)).fetchone()
    row_b = trace_b.db.execute("SELECT id, node, args, result, end_kind, exception FROM frames WHERE id = ?", (frame_b,)).fetchone()
    if row_a is None or row_b is None:
        raise LookupError(f"no frame #{frame_a if row_a is None else frame_b}")
    a, b = Side(trace_a), Side(trace_b)
    state = {"compared": 0, "cut": False, "first_arguments": None}
    root = (dict(row_a), dict(row_b))
    found = walk(a, b, root, [], state, limit, 0)
    if found is None and row_a["node"] == row_b["node"]:
        found = differ(a, b, root, [], same_args=None)
    elif found is None:
        found = {"kind": "call_path", "path": [], "a": row_a["id"], "b": row_b["id"], "note": "different functions"}
    result = {
        "a": trace_a.frame(frame_a),
        "b": trace_b.frame(frame_b),
        "divergence": expand(trace_a, trace_b, found) if found else None,
        "first_arguments": expand(trace_a, trace_b, state["first_arguments"]) if state["first_arguments"] else None,
        "compared": state["compared"],
        "cut": state["cut"],
    }
    return result


def walk(a: Side, b: Side, pair: tuple, path: list, state: dict, limit: int, depth: int) -> dict | None:
    """The first divergence below `pair` (two calls of one function), None when none."""
    if depth > 300 or state["compared"] >= limit:
        state["cut"] = True
        return None
    here = path + [step(a, b, pair)]
    kids_a, kids_b = a.children(pair[0]["id"]), b.children(pair[1]["id"])
    ops = align([row["node"] for row in kids_a], [row["node"] for row in kids_b])
    for index, (op, i, j) in enumerate(ops):
        if op != "match":
            row = kids_a[i] if op == "a_only" else kids_b[j]
            if repeated(ops, index, kids_a, row["node"]):
                # One more turn of a loop (a longer input): not a path of its own; what it
                # changed shows in the results that follow.
                extra = here[-1].setdefault("extra_calls", {})
                key = f"{'a' if op == 'a_only' else 'b'}:{row['node']}"
                extra[key] = extra.get(key, 0) + 1
                continue
            other = (kids_b[j] if j is not None and j < len(kids_b) else None) if op == "a_only" else (kids_a[i] if i is not None and i < len(kids_a) else None)
            return {"kind": "call_path", "path": here, "side": "a" if op == "a_only" else "b", "call": row["id"], "instead": other["id"] if other else None, "node": row["node"]}
        state["compared"] += 1
        child = (kids_a[i], kids_b[j])
        args_a, args_b = a.args(child[0]), b.args(child[1])
        same_args = None if args_a is None or args_b is None else args_a == args_b
        if same_args is False and state["first_arguments"] is None:
            state["first_arguments"] = {"kind": "arguments", "path": here + [step(a, b, child)], "a": child[0]["id"], "b": child[1]["id"], "node": child[0]["node"]}
        found = walk(a, b, child, here, state, limit, depth + 1)
        if found is not None:
            return found
        found = differ(a, b, child, here, same_args)
        if found is not None:
            return found
        if state["cut"]:
            return None
    return None


def repeated(ops: list[tuple], index: int, kids_a: list[dict], node: str) -> bool:
    """Whether an unmatched call is of the function of the nearest matched call before or after
    it, at the same level."""
    for direction in (-1, 1):
        at = index + direction
        while 0 <= at < len(ops) and ops[at][0] != "match":
            at += direction
        if 0 <= at < len(ops) and kids_a[ops[at][1]]["node"] == node:
            return True
    return False


def differ(a: Side, b: Side, pair: tuple, path: list, same_args: bool | None) -> dict | None:
    end_a, end_b = a.ending(pair[0]), b.ending(pair[1])
    if end_a == end_b:
        return None
    kind = "raised" if "raise" in (end_a[0], end_b[0]) else "result" if end_a[0] == end_b[0] == "return" else "ending"
    full = path if path and path[-1]["a"] == pair[0]["id"] else path + [step(a, b, pair)]
    return {"kind": kind, "path": full, "a": pair[0]["id"], "b": pair[1]["id"], "node": pair[0]["node"], "same_arguments": same_args}


def step(a: Side, b: Side, pair: tuple) -> dict:
    args_a, args_b = a.args(pair[0]) or {}, b.args(pair[1]) or {}
    changed = [name for name in {**args_a, **args_b} if args_a.get(name) != args_b.get(name)]
    return {"node": pair[0]["node"], "a": pair[0]["id"], "b": pair[1]["id"], "arguments_differ": changed}


def align(nodes_a: list[str], nodes_b: list[str]) -> list[tuple]:
    """Each level's calls in order: `("match", i, j)`, `("a_only", i, j_next)`, `("b_only",
    i_next, j)`, matched by function with difflib's longest matching blocks (by position when
    the lists are too long to compare)."""
    if len(nodes_a) * len(nodes_b) > MAX_ALIGN:
        ops = []
        for index in range(max(len(nodes_a), len(nodes_b))):
            if index < len(nodes_a) and index < len(nodes_b) and nodes_a[index] == nodes_b[index]:
                ops.append(("match", index, index))
            else:
                ops.append(("a_only", index, index) if index < len(nodes_a) else ("b_only", index, index))
                break
        return ops
    ops, i, j = [], 0, 0
    for block in difflib.SequenceMatcher(None, nodes_a, nodes_b, autojunk=False).get_matching_blocks():
        while i < block.a:
            ops.append(("a_only", i, j))
            i += 1
        while j < block.b:
            ops.append(("b_only", i, j))
            j += 1
        for offset in range(block.size):
            ops.append(("match", block.a + offset, block.b + offset))
        i, j = block.a + block.size, block.b + block.size
    return ops


def expand(trace_a: Trace, trace_b: Trace, found: dict) -> dict:
    """A divergence with its calls as Trace.frame() gives them (`call_a`, `call_b`)."""
    found = dict(found)
    if found["kind"] == "call_path" and "side" in found:
        mine, other = (trace_a, trace_b) if found["side"] == "a" else (trace_b, trace_a)
        found["call_a" if found["side"] == "a" else "call_b"] = mine.frame(found["call"])
        if found.get("instead"):
            found["call_b" if found["side"] == "a" else "call_a"] = other.frame(found["instead"])
    else:
        found["call_a"], found["call_b"] = trace_a.frame(found["a"]), trace_b.frame(found["b"])
    return found


def compare_tests(paths_a: list[Path], paths_b: list[Path] | None = None) -> dict:
    """Compare a failing test with a passing one: the first failing test of run A (else of run
    B), and the same test in the other run, else the same test function with other parameters,
    else the passing test whose executed functions are closest (Jaccard). Adds `test_a`,
    `test_b` (`{"nodeid", "pid", "frame", "outcome", "path"}`), `picked` (how B was chosen) and
    `swapped` (A is the second run's: only it had a failing test)."""
    with Recording(paths_a) as run_a, Recording(paths_b or paths_a) as run_b:
        tests_a, _ = test_runs(run_a)
        tests_b = tests_a if paths_b is None else test_runs(run_b)[0]
        failing = [test for test in tests_a if test["outcome"] == "failed" and test["frame"]]
        if not failing and paths_b is not None:
            run_a, run_b, tests_a, tests_b = run_b, run_a, tests_b, tests_a
            failing = [test for test in tests_a if test["outcome"] == "failed" and test["frame"]]
        if not failing:
            raise LookupError("no failing test with a recorded frame: give two frames to compare")
        test_a = failing[0]
        passing = [test for test in tests_b if test["outcome"] == "passed" and test["frame"]]
        if not passing:
            raise LookupError("no passing test to compare it with: give two frames to compare")
        test_b, picked = pick_partner(run_a, run_b, test_a, passing, paths_b is not None)
        found = compare(run_a.trace(test_a["path"]), test_a["frame"], run_b.trace(test_b["path"]), test_b["frame"])
        found["test_a"] = {key: str(test_a[key]) if key == "path" else test_a[key] for key in ("nodeid", "pid", "frame", "outcome", "path")}
        found["test_b"] = {key: str(test_b[key]) if key == "path" else test_b[key] for key in ("nodeid", "pid", "frame", "outcome", "path")}
        found["swapped"] = run_a.paths != [Path(path) for path in paths_a]  # A is run B's: only B had failing tests
        found["picked"] = picked
        return found


def pick_partner(run_a: Recording, run_b: Recording, test: dict, passing: list[dict], other_run: bool) -> tuple[dict, str]:
    if other_run:
        same = [other for other in passing if other["nodeid"] == test["nodeid"]]
        if same:
            return same[0], "the same test in the other run"
    base = test["nodeid"].split("[", 1)[0].split(" #", 1)[0]
    same = [other for other in passing if other["nodeid"].split("[", 1)[0].split(" #", 1)[0] == base and (other["node"] == test["node"] or other["node"] is None)]
    if same:
        return same[0], "the same test function with other parameters"
    mine = executed(run_a, test)
    scored = []
    for other in passing:
        theirs = executed(run_b, other)
        union = mine | theirs
        scored.append((len(mine & theirs) / len(union) if union else 0.0, other))
    score = max(similarity for similarity, _ in scored)
    tied = [other for similarity, other in scored if similarity == score][:50]
    if len(tied) == 1:
        return tied[0], f"the passing test whose functions are closest (Jaccard {score:.2f})"
    # The same functions on the same inputs: the divergence is then where behaviour differs, not
    # where the inputs do (a test of another item stops at the first call it passes another value).
    given = inputs(run_a, test)
    best = max(tied, key=lambda other: len(given & inputs(run_b, other)))
    return best, f"the passing test whose functions are closest (Jaccard {score:.2f}), with the most calls on the same inputs"
