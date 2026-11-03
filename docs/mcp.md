# Luciole for agents: `luciole mcp`

`luciole mcp` is an [MCP](https://modelcontextprotocol.io) server that lets an agent (Claude Code,
Cursor, any MCP client) run Python programs under Luciole's recorder and ask what happened,
instead of adding prints and running again. It runs on your machine, over stdio, and keeps
nothing outside `~/.luciole`.

## Add it to Claude Code

From your project's folder:

```sh
claude mcp add luciole -- luciole mcp            # luciole installed: pip install luciole
claude mcp add luciole -- uvx luciole mcp        # or without installing it
```

Add `--scope project` to share it with the project (it goes in `.mcp.json`). The server takes the
folder it starts in as the project root; `luciole mcp --root PATH` sets another.

`luciole mcp --runs DIR` reads the runs in `DIR` (a folder of run folders, or one run folder)
rather than the root's own, such as a CI artifact made by `pytest --luciole --luciole-dir DIR`;
`--no-run` leaves out the `run` tool, for a server that answers about existing recordings only
(post-mortem, see [post-mortem.md](post-mortem.md)): its instructions and answers then say what
the user should record again (`pytest --luciole --luciole-level 2 --luciole-scope F`) where they
would otherwise suggest `run`.

Recording needs the program to run on Python 3.12 or newer (`sys.monitoring`); the graph tools
work with any Python 3.10+.

## How an agent uses it

1. `run` a command: `python app.py --flag`, `pytest -x tests/test_cart.py`, `python -m pkg`. It
   runs as typed, with auto-start on: every Python process it starts (child processes, pool
   workers) records a trace at level 1, every call of project code with its arguments and return
   value, and every exception with where it was raised and handled. The answer has the exit
   status, the output, each process recorded, and the exceptions nothing in the project handled,
   with the frame (`#N`) they were raised in. It stays under about 3.5 KB: the output keeps its
   start and its end, and says which file has all of it (`output.txt` in the run's folder).
   Commands run through a package manager (`uv run`, `uvx`, `poetry run`, `pipenv run`, `hatch
   run`, `pdm run`, `conda run`, `rye run`, `pixi run`) are not run: they may build or sync an
   environment first, for minutes. The answer gives the command to run instead, with the
   project's `.venv` interpreter when there is one. A command that is not on PATH gets a way to
   run it with the interpreter: the project's console script of that name (from
   `pyproject.toml`, `setup.cfg` or `setup.py`) through `python -c`, else `python -m`.
2. Ask about the run:
   - `exceptions`: type, message, the function and line that raised it, its chain, where it was
     handled;
   - `traceback`: the frames an exception went through, innermost first, each with its local
     variables as the exception left it, the view a post-mortem debugger gives, with no re-run.
     By default, the original error behind the last exception nothing handled: the first of its
     chain (`raise ... from`, or raised while handling another), where the problem started, not
     the wrapper that reached the top. The chain is listed one line per exception, and
     `exception: N` shows another;
   - `calls FUNCTION [caller] [detail]`: its calls grouped by call site (`from pkg.mod:fn:85
     ×38`, and the call sites not on the page), each with its return value or exception first,
     then its arguments. Arguments with the same value in every call shown are said once,
     `self` and other objects without a repr of their own are shortened (`<Cart object …7cb0>`),
     long values are cut (`frame` shows a call's in full), and consecutive calls that read the
     same are one line (`#7..#26 ×20`). `caller` keeps the calls made by one function; `detail`
     gives one line per call with every value as recorded;
   - `call_tree` (from the top, or below a frame), repeated calls collapsed into `×N`, and calls
     that took under 1% of the time (and under a tenth of their caller's) folded into one line
     per caller, unless something raised under them. `focus: "Cart.price"` keeps only the paths from the top to that function's
     calls (`*` as a wildcard), the other calls counted, not shown;
   - `frame N`: one call in detail, and the calls it made;
   - `tests`: for a test run recorded with `pytest --luciole`, or any pytest run under `run`
     (it sets `LUCIOLE_PYTEST=1`, which turns the plugin on), each test's outcome, failed ones first, with where it failed (setup, call,
     teardown), the message, its frame (`#N`), the exception it ended with (`!N`) and its process,
     to pass to `traceback`, `frame` and `origin`;
   - `suspects [top]`: for a run with failing tests, the functions ranked by how much failing
     tests ran them and passing ones did not (Ochiai), with the tests that ran each, its file and
     line, and for the first ones a call made in a failing test that no passing test made, beside
     the closest passing call. The place to start when `tests` shows failures;
   - `compare [frame_a frame_b] [run_b]`: the first point where two executions differ (a call
     that returned something else, raised on one side only, or was made on one side only), with
     both sides' values and the path from the top. Without frames, the first failing test against
     the same test in `run_b`, else the same test function with other parameters, else the passing
     test that ran the most similar functions. Both are explained in
     [fault-localization.md](fault-localization.md);
   - `summary`: duration, exit status, top functions by calls and by time (self time leaves out
     the time coroutines spend awaiting);
   - `variable_history FUNCTION [variable] [frame]`: every assignment to its local variables,
     call by call and line by line, with the values, each call ending with what it returned or
     raised and at which line. A variable assigned once per call to the same value in every call
     is said once. It needs a run with a `scope` covering the function (below); otherwise it
     says so, and how to get one;
   - `origin FUNCTION VARIABLE [frame] [at] [depth]`: where a wrong value came from, walked back
     across functions: the assignment that set it, the caller that passed it, the call that
     returned it, the last call that set a global or an attribute of the same object, then what
     fed those, each with its line, code and value (see [origins.md](origins.md)). `VARIABLE` is a
     local or parameter, `return`, `self.attr`, or a global the function reads. Values that crossed
     calls are known at level 1; for a local assigned on several lines, the answer names the
     function to put in a `scope` to know which line ran.
3. When a wrong value was made inside a function, long before anything failed (so that the
   locals `traceback` shows are not enough), `run` the same command again with `scope`, the
   suspect functions: `scope: ["cart:checkout", "Cart.*"]` (node ids or their ends, `*` as a
   wildcard). That is trace level 2: level 1 everywhere, plus every assignment in those functions
   (rebinding, and changes to lists, dicts, sets and bytearrays), about 1.7 µs per line they run
   and nothing elsewhere. `level: 2` without a `scope` is refused: recording every line of every
   function would be far too slow.
4. Ask the static graph, for code that did not run too: `find NAME` (where it is), `neighbors
   FUNCTION` (who calls it, what it calls, where its parameters' values come from, the globals and
   attributes it reads and writes, where its return value goes).

Each run is kept (the last 50 per project): `runs` lists them, and every query takes `run` (default:
the last) and `process` (a pid; default: the first process). Long answers are cut and paginated
(`limit`, `offset`, `depth`). An answer ends with at most one line naming the most useful next
query, when there is an obvious one (`traceback` after a run that failed, `focus` after a folded
call tree, `frame` when values were cut).

The instructions the server sends at initialize tell the agent to use `run` when it needs
values or call paths a print would not give easily, to verify fixes with plain pytest rather
than under the recorder, to stop recording once `calls` or `traceback` pinpoints the cause, and
to check who calls the function it blames before choosing where to fix, and, when tests fail, to
start with `suspects` and `compare`.

With the VS Code extension open, the engine it runs is found too: the brain lights up while the
agent's programs run.

## Where runs are kept

`~/.luciole/runs/<project>-<hash>/<run id>/`: one trace per process (`*.sqlite`, see
[trace-format.md](trace-format.md)), `command.json` and `output.txt` (the program's output).
`<project>` is the root folder's name and `<hash>` the start of the SHA-1 of its absolute path; a
run id is the time it started (`20261030-142501`, then `-1`, `-2`... in the same second), so ids
sort as runs started. The VS Code extension's Run Live records into the same folders (without
`output.txt`: the program's output is in its terminal), so its Calls view lists the agent's runs
next to yours, and `runs` lists yours to the agent. Both write them with
[`src/luciole/runs.py`](../src/luciole/runs.py).

`command.json`: `command` (a list), `cwd`, `level`, `scope`, `by` (`agent`, `vscode` for Run
Live, `pytest` for `pytest --luciole`; missing in runs made before it existed, which were the MCP server's), `started_at` (Unix
time), and once the run ended, `exit_status` (null when unknown or stopped by a timeout) and
`ended_at`.

```sh
luciole runs [--root DIR] [--json]       # the runs, newest first, each with its processes
luciole trace FILE ...                   # one process's trace
```

`luciole runs --json` gives `{"root", "folder", "runs"}`, each run being its `command.json` plus
`id`, `folder`, `state` (`running` until whoever started it says it ended; then `finished`, or
`empty` when nothing was recorded, as with Python older than 3.12) and `processes` (file, pid,
argv, duration, exit status, numbers of calls, exceptions and uncaught exceptions, level).
`luciole runs new --root DIR --command JSON [--cwd DIR] [--by vscode]` makes a run folder and
prints it; `luciole runs finish FOLDER [--exit-status N]` marks it ended.

## Protocol

MCP over stdio (JSON-RPC 2.0, one message per line), protocol versions 2025-06-18, 2025-03-26 and
2024-11-05, tools only. Tool results are text meant for a model; errors (a wrong run id, a
function that did not run) come back as tool errors that say what to do instead.
