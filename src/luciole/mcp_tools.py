"""The MCP server's instructions and tool definitions (mcp.py serves them): what an agent reads
before it calls anything."""

from __future__ import annotations

import json

INSTRUCTIONS = """\
Luciole records Python programs (3.12+) so you can debug from what actually happened. `run`
executes a command under the recorder: every call of project code with its arguments and return
value, every exception with where it was raised and handled, and the local variables of every
frame an exception left. Use it when you need values or call paths that a print would not give
easily; verify fixes with plain pytest in your shell, not under the recorder. Run the interpreter
itself (`python -m pytest -x tests/test_x.py`), not `uv run` or `poetry run`.
Then ask: `traceback` (the original error, each frame's locals), `calls` (a function's calls
grouped by caller, return values first), `call_tree` (`focus` keeps the paths to one function),
`frame` (one call in full), `origin` (where a wrong value came from, across functions),
`variable_history` (every assignment in a function, after a `run` with `scope` naming it). `find`
and `neighbors` answer from the static graph, even for code that did not run.
When tests fail (a pytest run under `run` records each test's outcome; `tests` lists them),
start with `suspects`: the functions failing tests ran and passing ones did not, each with a call
no passing test made. `compare` sets a failing test beside the closest passing one (or any two
frames) and shows where they first differ: a result, an exception, a call made on one side only.
Stop recording once `calls` or `traceback` pinpoints the cause. Before choosing where to fix,
check who calls the function you blame (`calls` lists its callers): the fix may belong there.
Runs are kept: `runs` lists them, and every query takes a run id (default: the last).
"""

POST_MORTEM = """\
Luciole recorded Python programs (3.12+), usually a test run kept because tests failed
(`pytest --luciole`, often in CI): every call of project code with its arguments and return value,
every exception with where it was raised and handled, and the local variables of every frame an
exception left. This server reads those recordings only: it cannot run programs. Debug from what
happened. Start with `tests` (the failures, each with its frame #N, exception !N and process),
then `suspects` (the functions failing tests ran and passing ones did not, each with a call no
passing test made) and `compare` (a failing test beside the closest passing one: where they first
differ, a result, an exception, a call made on one side only). Then ask: `traceback` (the original error, each frame's locals), `calls` (a function's calls
grouped by caller, return values first), `call_tree` (`focus` keeps the paths to one function),
`frame` (one call in full), `origin` (where a wrong value came from, across functions),
`variable_history` (only for functions the run was recorded with in its scope). `find` and
`neighbors` answer from the static graph of the checkout, which should be the commit that ran.
When the recording lacks something (which line set a local, a variable's history), say what the
user should record again (`pytest --luciole --luciole-level 2 --luciole-scope FUNCTION`) rather
than guessing. Before choosing where to fix, check who calls the function you blame (`calls` lists
its callers): the fix may belong there.
Runs are kept: `runs` lists them, and every query takes a run id (default: the last).
"""

# Post-mortem mode: tool descriptions say what to record again rather than to `run`.
NO_RUN_TEXT = {
    "A run id from `run` or `runs`": "A run id from `runs`",
    "when it was not, says so: then `run` the command again with `scope: [\"<function>\"]`.": "when it was not, says so, and what to record again.",
    "the answer says so, and how to re-run.": "the answer says so, and what to record again.",
    "(`pytest --luciole`, or pytest under `run`)": "(`pytest --luciole`)",
}

FRAME_ARG = {"type": "integer", "description": "A frame id (#N in other answers)."}
RUN_ARG = {"type": "string", "description": "A run id from `run` or `runs` (default: the last run)."}
PROCESS_ARG = {"type": "integer", "description": "A process id within the run (default: its first process)."}
PAGE_ARGS = {
    "limit": {"type": "integer", "description": "How many to show (default 20)."},
    "offset": {"type": "integer", "description": "How many to skip (default 0)."},
}

TOOLS = [
    {
        "name": "run",
        "description": "Run a command under Luciole's recorder (Python 3.12+) and record what happens: every call of project code with its arguments and return value, every exception. Returns the exit status, the output, and the exceptions that escaped. Child processes are recorded too. With `scope`, also every assignment to the local variables of those functions (for `variable_history`). Give the interpreter's command: `uv run`, `poetry run` and the like are not run (they may build an environment first).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command, as typed in a shell (no pipes): `python app.py --flag`, `pytest -x tests/test_cart.py`, `python -m pkg`."},
                "cwd": {"type": "string", "description": "Folder to run in (default: the project root)."},
                "level": {"type": "integer", "enum": [0, 1, 2], "description": "1 (default): arguments and return values too. 0: calls and exceptions only, cheaper. 2 (the default with a `scope`): level 1, plus every assignment in the `scope` functions."},
                "scope": {
                    "type": ["string", "array"],
                    "items": {"type": "string"},
                    "description": "Functions whose local variables to record, line by line (level 2): node ids or their ends (`cart:checkout`, `Cart.add`, `checkout`), `*` as a wildcard (`cart:Cart.*`), as a list or comma-separated. Keep it to the few suspect functions: each line they run costs a few microseconds.",
                },
                "timeout": {"type": "number", "description": "Seconds before the program is stopped (default 600)."},
            },
            "required": ["command"],
        },
    },
    {"name": "runs", "description": "The recorded runs of this project, newest first.", "inputSchema": {"type": "object", "properties": {}}},
    {
        "name": "summary",
        "description": "A run's process: its command, duration and exit status, and its top functions by calls and by time.",
        "inputSchema": {"type": "object", "properties": {"run": RUN_ARG, "process": PROCESS_ARG}},
    },
    {
        "name": "exceptions",
        "description": "Exceptions in a run, in the order they were raised: type, message, the function and line that raised it, what it was chained from, and where it was handled (or not).",
        "inputSchema": {"type": "object", "properties": {"run": RUN_ARG, "process": PROCESS_ARG, **PAGE_ARGS}},
    },
    {
        "name": "traceback",
        "description": "The frames an exception went through, innermost first: for each, the line it was at and its local variables at that moment, then where it was handled. Default: the original error behind the last exception nothing handled (the first of its chain of `raise ... from` and exceptions raised while handling another), with the chain listed one line each.",
        "inputSchema": {"type": "object", "properties": {"exception": {"type": "integer", "description": "An exception id (!N in other answers)."}, "run": RUN_ARG, "process": PROCESS_ARG}},
    },
    {
        "name": "calls",
        "description": "The calls of a function in a run, grouped by call site (who called it, from which line, how many times): each call's return value or exception first, then its arguments. Arguments with the same value in every call shown are said once, values are cut short (`frame` shows one call's in full), and consecutive calls that read the same are one line. `function` is a node id (`pkg.mod:Class.method`) or its end (`Class.method`, `method`).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function": {"type": "string"},
                "caller": {"type": "string", "description": "Only the calls made by this function (a node id or its end)."},
                "detail": {"type": "boolean", "description": "One line per call with every value as recorded (default false)."},
                "run": RUN_ARG,
                "process": PROCESS_ARG,
                **PAGE_ARGS,
            },
            "required": ["function"],
        },
    },
    {
        "name": "call_tree",
        "description": "The call tree of a run (or below one frame), repeated calls collapsed into `×N`, calls that took under 1% of the time (and under a tenth of their caller's) folded into one line per caller, unless something raised under them. With `focus`, only the paths from the top to the calls of the matching functions, the other calls counted, not shown.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "frame": FRAME_ARG,
                "focus": {"type": "string", "description": "A function name or node id (`checkout`, `Cart.add`), `*` as a wildcard (`Cart.*`): show only the paths that lead to its calls."},
                "depth": {"type": "integer", "description": "Levels shown (default 4); with `focus`, levels shown below each matching call (default 1)."},
                "run": RUN_ARG,
                "process": PROCESS_ARG,
            },
        },
    },
    {
        "name": "frame",
        "description": "One call in detail: its arguments, result or exception, caller, and the calls it made.",
        "inputSchema": {"type": "object", "properties": {"frame": FRAME_ARG, "run": RUN_ARG, "process": PROCESS_ARG}, "required": ["frame"]},
    },
    {
        "name": "variable_history",
        "description": "Every assignment to a function's local variables during a run, call by call and line by line, with the value assigned (rebinding, and changes to lists, dicts and sets), each call ending with what it returned or raised, at which line: how a value was built. A variable assigned once per call to the same value in every call is said once. Needs a run made with `scope` covering the function (level 2); when it was not, says so: then `run` the command again with `scope: [\"<function>\"]`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function": {"type": "string", "description": "A node id (`pkg.mod:Class.method`) or its end (`Class.method`, `method`)."},
                "variable": {"type": "string", "description": "One variable only."},
                "frame": FRAME_ARG,
                "run": RUN_ARG,
                "process": PROCESS_ARG,
                "limit": {"type": "integer", "description": "How many assignments to show (default 50)."},
                "offset": {"type": "integer", "description": "How many to skip (default 0)."},
            },
        },
    },
    {
        "name": "origin",
        "description": "Where a value came from. From one variable of one call, walks back through what made it, each step with its line, code and value: the assignment that set it, the caller that passed it (a parameter), the call that returned it, the last call that set a global or an attribute of the same object; then what fed those, across functions. Use it on a wrong value (a wrong result, a bad argument or local in `traceback`) rather than guessing. A local assigned on several lines needs its function in a `scope` (level 2) to know which line ran: the answer says so, and how to re-run. The data paths come from static analysis: calls through objects of unknown type, and changes made inside containers, can be missing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function": {"type": "string", "description": "A node id (`pkg.mod:Class.method`) or its end (`Class.method`, `method`). Not needed with `frame`."},
                "variable": {"type": "string", "description": "A local or parameter (`due`), `return` (what the call returned), an attribute of self (`self.total`), a global the function reads (`RATE`), or a variable's node id (`pkg.mod:Class#total`)."},
                "frame": {"type": "integer", "description": "Which call (#N in other answers; default: the function's last call)."},
                "at": {"type": "integer", "description": "The value at this moment, in ns since the run started, as given where a walk was cut (default: the call's end)."},
                "depth": {"type": "integer", "description": "Steps followed back (default 8)."},
                "run": RUN_ARG,
                "process": PROCESS_ARG,
            },
            "required": ["variable"],
        },
    },
    {
        "name": "tests",
        "description": "The tests of a recorded test run (`pytest --luciole`, or pytest under `run`), failed ones first: outcome, where it failed (setup, call or teardown), the message, and for each its frame (#N), the exception it ended with (!N) and its process (pid), to pass to `traceback`, `frame` and `origin`.",
        "inputSchema": {"type": "object", "properties": {"run": RUN_ARG, "failed": {"type": "boolean", "description": "Failed and errored tests only."}, **PAGE_ARGS}},
    },
    {
        "name": "suspects",
        "description": "For a run with failing tests (start here when `tests` shows failures): the project functions ranked by how much the failing tests ran them and the passing tests did not (spectrum-based fault localization, Ochiai: failed(f) / sqrt(all failed × (failed(f) + passed(f)))), each with the tests that ran it, its file and line, and for the first ones a call made in a failing test that no passing test made, beside the closest passing call. Works on the recording, without running anything; tests' outcomes come from pytest (inferred from test functions' frames otherwise), across processes (pytest-xdist).",
        "inputSchema": {"type": "object", "properties": {"run": RUN_ARG, "top": {"type": "integer", "description": "How many functions to show (default 10)."}}},
    },
    {
        "name": "compare",
        "description": "The first point where two executions differ: a failing case and a passing one. Walks both call trees side by side (calls matched by function, extra loop turns tolerated) and reports the first call that returned something else, raised on one side only, or a call made on one side only, with both sides' values and the path from the top; arguments that differ are shown along the path. Without frames, compares the first failing test with the same test in `run_b`, else the same test function with other parameters, else the passing test that ran the most similar functions. Give `frame_a` and `frame_b` (#N, e.g. two tests' frames from `tests`, or two calls from `calls`) to compare any two calls, in one run or two.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "frame_a": {"type": "integer", "description": "A frame id in `run` (with `frame_b`)."},
                "frame_b": {"type": "integer", "description": "A frame id in `run_b` (default: the same run)."},
                "run": RUN_ARG,
                "process": PROCESS_ARG,
                "run_b": {"type": "string", "description": "A second run, e.g. the same tests passing before a change (default: `run`)."},
                "process_b": {"type": "integer", "description": "B's process (default: `process` in the same run, else the first)."},
            },
        },
    },
    {
        "name": "find",
        "description": "Find functions and classes in the project's static graph by name: their node id, file and line.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "A name, or the end of a node id (`Cart.total`)."}}, "required": ["name"]},
    },
    {
        "name": "neighbors",
        "description": "From the static graph, around one function: who calls it, what it calls, its parameters and where their values come from, which globals and attributes it reads and writes, and where its return value goes.",
        "inputSchema": {"type": "object", "properties": {"function": {"type": "string"}}, "required": ["function"]},
    },
]


def no_run_tools() -> list[dict]:
    """The tools without `run`, their descriptions saying what to record again instead."""
    found = []
    for tool in TOOLS:
        if tool["name"] == "run":
            continue
        text = json.dumps(tool)
        for old, new in NO_RUN_TEXT.items():
            text = text.replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1])
        found.append(json.loads(text))
    return found
