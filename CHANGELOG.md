# Changelog

## Unreleased

- Variable history (trace level 2): `luciole run --trace FILE --scope cart:checkout,Cart.*`
  records every assignment to the local variables of the functions named (node ids or their
  ends, `*` as a wildcard): rebinding, and changes to lists, dicts, sets and bytearrays, each with
  its line, time and value (secrets redacted). Only those functions pay for it (LINE events are
  turned on for their code alone): about 1.7 µs per line they run, nothing elsewhere. `luciole
  trace FILE history NODE [--var NAME]` reads it, and so does the MCP tool `variable_history`,
  whose `run` takes a `scope`; it says when a run has no history for a function and how to get
  one. Also `luciole.start(scope=...)`, `luciole env --scope` and `LUCIOLE_TRACE_SCOPE`; at most
  1,000,000 writes per run (`LUCIOLE_TRACE_MAX_WRITES`).
- Time a coroutine spends awaiting (or a generator suspended at a `yield`) is no longer counted as
  busy: frames record it (`suspended_ns`), and `luciole trace FILE summary` shows each function's
  active time, its self time being its active time minus its callees'. In `examples/workers.py`,
  `download` (which awaits `asyncio.sleep`) went from 3.6 s of self time to under a millisecond.

- Failure snapshots: at level 1, a frame that an exception leaves keeps its local variables as
  they were then (secrets redacted, 50 per frame, 10,000 frames per run). `luciole trace FILE
  traceback [ID]` and the MCP tool `traceback` show the frames an exception went through with
  their locals, as a post-mortem debugger would, without running the program again.
- Traces of programs that raise and catch many exceptions no longer take seconds to finish: 20,000
  caught exceptions took 10 s at exit, now a fraction of a second.
- `luciole mcp`: an MCP server for agents (Claude Code, Cursor, any MCP client). `run` runs a
  command as typed (a script, pytest, a module) with every Python process it starts recording a
  trace at level 1, and answers with the exit status, the output and the exceptions nothing
  handled; `exceptions`, `calls`, `call_tree`, `frame`, `summary` and `runs` query the runs, and
  `find` and `neighbors` the static graph. See `docs/mcp.md`.

## 0.2.0

- Right-click on a neuron adds or removes its breakpoint on big graphs too, where a neuron is a
  few pixels wide: the click counts where the button went down, a few pixels of jitter are
  allowed, and a click next to a small neuron picks the nearest one.
- `luciole run` on a script that does not exist says so in one line, instead of a traceback and an
  empty trace file.
- Graphs of 60 functions or fewer (whatever the number of files) are laid out in rows by call
  order: what nothing calls on top, each function above the ones it calls, so a file reads like
  an outline; there are no region names. Lines between functions of one row arc above it, lines
  that skip rows bow to the side only to go around a function in their way. Graphs of more than
  60 functions keep the map of regions.
- Big graphs turn smoothly, on modest machines too: all spheres are drawn in one batch and all
  lines in another (on `rich`, 38 draw calls per frame instead of 6,101), each frame only redoes
  what changed, and a dragged neuron no longer makes the whole brain recompute while it slides
  back. A still brain is not drawn at all (no GPU or CPU spent) until input, a run or a change
  wakes it. Looks the same.
- Without the Python Debugger extension, Run Live still runs (in a terminal, lighting only), and
  the panel now says so, with a button that installs the debugger, until it is installed. This
  replaces a notification that showed once ever.
- Live mode: a process that ends within milliseconds of starting (a multiprocessing pool's
  workers, terminated right after their last task) no longer loses its calls. The recorder now
  reads the engine's welcome before the program goes on (waiting half a second at most); left
  unread, it made the connection reset when the process ended, and the engine dropped the calls
  it had not read yet. A worker terminated while it was itself exiting no longer dies without
  sending its calls either.
- ⚡ Run Live on a file of a package that imports relatively (`from . import x`) runs it as a
  module (`python -m pkg.mod`), the only way it can run; it stopped at its first relative import.
- A Luciole sidebar in the activity bar, with three native views. **Actions**: Show Graph (file
  or folder), Run Live, Run Live… and the auto-start switch with its state. **Running**: the
  processes lit up now (Run Live, auto-started programs, tests, child processes) by command line,
  pid and state, plus the last few that exited; Stop for programs outside VS Code's debug
  sessions, a button to the Call Stack (where their sessions are) for the ones in them. **Map**:
  an outline of the graph in the panel (packages, modules, classes, functions: the brain's own
  regions), with counts and running functions marked; clicking an item flies the brain to it and
  marks it, functions have Go to Source, and **Luciole: Find in Map** lists every function.

- Each file's functions get a muted tint of their own (Apple's system hues, half-way to grey), so you see which module a function is in; activity stays in the accent.
- A calmer, minimal look: neutral greys at rest and one accent color for activity, the theme's
  own (its link color), with red only for breakpoints and the debugger. Smaller matte neurons,
  hairline links, names in the system font with no outlines; no colors per region, no glows.
  A running function fills with the accent and gets a thin ring, a coroutine waiting on an
  `await` gets the ring only. Works in light and dark themes. The legend is gone: hover a line to
  see what it is.
- Live mode: a call traces its line from the caller to the callee in the accent color, like a
  route drawing itself on a map (instead of bubbles), then eases back to grey. A busy line stays
  drawn until its calls stop.
- The brain is a map: packages, modules and classes are regions inside regions, placed by how
  much they call each other, with their names at their centres. The same project always gives
  the same map, and editing a file leaves the rest in place. Calls between regions travel
  together in tracts instead of thousands of crossing lines.
- Run Live (⚡, Run Live…, run configurations, tests for a file, Run again) runs the program under
  VS Code's Python debugger, lit up by the recorder: breakpoints, stopping on exceptions, stepping,
  the Variables, Watch and Call Stack views, hover and the Debug Console all work on it. Without
  the Python Debugger extension (`ms-python.debugpy`), it runs in a terminal as before, lighting
  only. Luciole's own Pause/Resume and Step commands, their keys (F10, F11, Shift+F11, F5) and
  buttons, and the Luciole Variables view are removed: the debugger's do the same. The recorder
  keeps its pause, step and variables protocol for other clients of the engine.
- Traces: `luciole run --trace run.sqlite` keeps a record of the run in a SQLite file: every call
  of project code with its caller, call site, thread and timings, and every exception (where it
  was raised, what it was chained from, where it was handled). `--level 1` adds each call's
  arguments and return value, as short snapshots, with secrets (`password`, `token`, `api_key`…)
  redacted. `luciole trace run.sqlite summary|calls NODE|tree|exceptions` reads it, as text or
  `--json`. With auto-start, `luciole env --trace DIR` (or `LUCIOLE_TRACE=DIR`) makes every
  Python process write its own trace, child processes included. `--trace DIR/` makes a new file
  in `DIR`, creating it. If the file cannot be written (a full disk), tracing stops with a
  warning and the program runs on. The format is documented in `docs/trace-format.md`.
- Auto-start: with the **Luciole: on** switch (status bar, setting `luciole.autoStart`), every
  Python program started in a new VS Code terminal lights up the view, child processes included
  (forked ones too). Nothing is started when the view is not listening. `luciole env` does the
  same for any shell, and `luciole.start()` / `luciole.stop()` from inside a program.
- Run Live on a library module (no `if __name__ == "__main__":`) runs the test files that import
  it with pytest, and on a test file runs pytest on it. `luciole tests FILE` lists those tests.
- The Luciole engine (`luciole engine`): one local process that recording programs connect to and
  views subscribe to, started by the extension when none runs and found through
  `~/.luciole/engine-v1.json`. Every connection starts with a handshake that checks the protocol
  version. Each VS Code window sees the programs of its own folders. `luciole run` and
  `luciole env` no longer need a port.
- Debugging: the brain follows VS Code's Python debugger (the function it stopped in glows red),
  right-click a neuron to stop on each call of its function, and with auto-start on, debugged
  programs light up too. Function nodes gain `body_line`, the first line each call runs.
- Better call resolution: imports (which run the imported module), star imports, properties,
  `super()`, overrides in subclasses, return types and annotations, special methods (`x[i]`,
  `with`, `len`, f-strings…), functions held in variables, and, as a marked guess (`by_name`), the
  project's only method of a name. On the standard library's `email` and `asyncio` packages, the
  share of runtime calls missing from the graph went from 52% to 27%.
- Callbacks: a function passed, not called, as an argument (`Thread(target=crunch)`,
  `pool.submit(work)`, `sorted(key=rank)`, `button.bind(self.on_click)`) is linked to the function
  passing it (`calls` edges with `"callback": true`), so threads and handlers no longer float apart.
- The view shows several processes at once; Stop applies to all of them (terminal runs).
- The graph and `luciole tests` skip the folders the project's `.gitignore` lists (data, runs,
  outputs), and `luciole tests` also finds tests that reach a module through other modules.
- A process ended by SIGTERM (a `multiprocessing` pool's workers) still reports its last calls.
- Labels sit above their neuron, and overlapping labels give way to the more important one.
- The recorder uses a `sys.monitoring` tool id of its own, so `cProfile`, coverage and debuggers
  keep working with it.

## 0.1.0

First release.

- `luciole graph`: functions, classes, variables, calls and variable flow of a Python project,
  from its source.
- `luciole run`: runs a script or module under a recorder (`sys.monitoring`, Python 3.12+) that
  reports calls, call paths, threads and asyncio tasks, and can pause, step and show variables.
- VS Code extension: a 3D graph of the project, lit up as the program runs. Run Live on the
  current file, Run Live… for arguments, modules (pytest) and run configurations, and Stop,
  Pause/Resume, Step over/into/out with a Variables view.
