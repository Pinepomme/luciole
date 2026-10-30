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

Recording needs the program to run on Python 3.12 or newer (`sys.monitoring`); the graph tools
work with any Python 3.10+.

## How an agent uses it

1. `run` a command: `python app.py --flag`, `pytest -x tests/test_cart.py`, `python -m pkg`. It
   runs as typed, with auto-start on: every Python process it starts (child processes, pool
   workers) records a trace at level 1, every call of project code with its arguments and return
   value, and every exception with where it was raised and handled. The answer has the exit
   status, the output (its start and end), each process recorded, and the exceptions nothing in
   the project handled, with the frame (`#N`) they were raised in.
2. Ask about the run:
   - `exceptions`: type, message, the function and line that raised it, its chain, where it was
     handled;
   - `traceback` (default: the last exception nothing handled): the frames it went through,
     innermost first, each with its local variables as the exception left it, the view a
     post-mortem debugger gives, with no re-run;
   - `calls FUNCTION`: every call, with arguments, return value or exception, caller, duration;
   - `call_tree` (from the top, or below a frame), repeated calls collapsed into `×N`;
   - `frame N`: one call in detail, and the calls it made;
   - `summary`: duration, exit status, top functions by calls and by time (self time leaves out
     the time coroutines spend awaiting);
   - `variable_history FUNCTION [variable] [frame]`: every assignment to its local variables,
     call by call and line by line, with the values. It needs a run with a `scope` covering the
     function (below); otherwise it says so, and how to get one.
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
(`limit`, `offset`, `depth`).

With the VS Code extension open, the engine it runs is found too: the brain lights up while the
agent's programs run.

## Where runs are kept

`~/.luciole/runs/<project>-<hash>/<run id>/`: one trace per process (`*.sqlite`, see
[trace-format.md](trace-format.md)), `command.json` (command, folder, level, scope, exit status) and
`output.txt` (the program's output). `luciole trace FILE ...` reads the traces too.

## Protocol

MCP over stdio (JSON-RPC 2.0, one message per line), protocol versions 2025-06-18, 2025-03-26 and
2024-11-05, tools only. Tool results are text meant for a model; errors (a wrong run id, a
function that did not run) come back as tool errors that say what to do instead.
