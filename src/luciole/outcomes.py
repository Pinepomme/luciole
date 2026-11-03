"""Test outcomes of a recorded run, across its processes: the `tests` table the pytest plugin
fills (pytest_plugin.py; pytest-xdist workers each write their own trace). For `luciole trace
FILE|FOLDER tests` and the MCP server's `tests`.

    run_tests([trace files])     {"processes", "recorded", "counts", "tests": [...]}, failed first
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from luciole.trace import FAILED, TEST_OUTCOMES, Trace, TraceError, plural


def run_tests(paths: list[Path], failed: bool = False) -> dict:
    """The tests in these traces (one per process). `counts` counts each test once, by its last
    attempt (a flaky test rerun until it passed is passed); `tests` lists every attempt, the tests
    that failed first, then attempts that failed before a rerun, then the others, each in the
    order they ended, with the `pid` and `file` of its process and `final`: whether it is the
    test's last attempt. `failed`: failed and errored attempts only. `recorded` counts the
    traces with outcomes (none: not a test run, or pytest ran without --luciole)."""
    found, recorded = [], 0
    for path in paths:
        try:
            with Trace(path) as trace:
                tests = trace.tests()
                pid = trace.run()["pid"]
        except (TraceError, sqlite3.Error):
            continue
        if tests is None:
            continue
        recorded += 1
        found += [{**test, "pid": pid, "file": str(path)} for test in tests]
    last: dict[tuple, int] = {}
    for test in found:
        key = (test["pid"], test["nodeid"])
        last[key] = max(last.get(key, 0), test["attempt"])
    counts = dict.fromkeys(TEST_OUTCOMES, 0)
    for test in found:
        test["final"] = test["attempt"] == last[(test["pid"], test["nodeid"])]
        if test["final"]:
            counts[test["outcome"]] = counts.get(test["outcome"], 0) + 1
    rank = lambda test: 0 if test["outcome"] in FAILED and test["final"] else 1 if test["outcome"] in FAILED else 2  # noqa: E731
    found.sort(key=rank)  # stable: in the order they ended within each rank
    if failed:
        found = [test for test in found if test["outcome"] in FAILED]
    return {"processes": len(paths), "recorded": recorded, "counts": {key: n for key, n in counts.items() if n}, "tests": found}


def render_tests(found: dict, limit: int = 50, offset: int = 0) -> str:
    if not found["recorded"]:
        return "no test outcomes recorded: not a test run, or pytest ran without --luciole"
    counts = found["counts"]
    total = sum(counts.values())
    many = found["recorded"] > 1
    head = f"{plural(total, 'test')}" + (f" in {plural(found['processes'], 'process')}" if many else "")
    lines = [head + ": " + ", ".join(f"{n} {outcome}" for outcome, n in counts.items()) if counts else head]
    tests = found["tests"]
    for test in tests[offset : offset + limit]:
        marks = [f"({test['phase']})"] if test["outcome"] in FAILED else []
        if test["attempt"] > 1 or not test["final"]:
            marks.append(f"attempt {test['attempt']}" + ("" if test["final"] else ", rerun"))
        if test["frame"]:
            marks.append(f"#{test['frame']}")
        if test["exception"]:
            marks.append(f"!{test['exception']['id']}")
        if many:
            marks.append(f"pid {test['pid']}")
        lines.append(f"  {test['outcome']:<7} {test['nodeid']}  {'  '.join(marks)}".rstrip())
        if test["message"] and test["outcome"] != "passed":
            message = test["message"].strip().splitlines()
            lines += [f"          {line}" for line in message[:3]]
            if len(message) > 3:
                lines.append(f"          ... {plural(len(message) - 3, 'more line')}")
    shown = len(tests[offset : offset + limit])
    if offset + shown < len(tests):
        lines.append(f"  ... {len(tests) - offset - shown} more (--limit, --offset)")
    return "\n".join(lines)
