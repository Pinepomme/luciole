# Luciole roadmap

Make Python excellent for one real user first, then build what makes Luciole different (agents
and time travel), and only then widen to other editors and languages.

## What exists

- **Graph format** ([graph-format.md](graph-format.md)) and the **static analyzer**
  (`luciole graph`): functions, classes, variables, calls and variable flow, for Python.
- **VS Code extension**: a 3D view where each function is a neuron and the variables passed
  between functions are the lines connecting them. It graphs the current file or a whole folder.
- **Live mode** (`luciole run`, ⚡ Run Live): functions light up as they run, with the current
  call path, pulses along calls, threads and asyncio tasks, and calls found only at runtime.
- **Run Live under VS Code's Python debugger**: breakpoints, stepping, variables and the debug
  console are the debugger's while the recorder lights up the view (a plain terminal run without
  the Python Debugger extension). The recorder also has its own pause/step/variables protocol,
  for engine clients.
- **The engine** (`luciole engine`) and **auto-start**: any Python process (terminal runs, test
  runners, child processes, debug sessions) lights up the view, with no change to how it starts.
- **Debugging with the graph**: VS Code's Python debugger and the brain together (the stopped
  function glows red, breakpoints from neurons).
- **Call resolution** without a type checker, including callbacks (`Thread(target=f)`): on the
  standard library's `email` and `asyncio`, missing runtime calls went from 52% to 27%.
- **A Luciole sidebar**: actions, running programs, and a map outline with search that flies to
  the function in the brain.
- **The trace store** (`luciole run --trace`, `luciole trace`): one SQLite file per process with
  the call tree, arguments and return values, and exceptions.

## Target architecture

One **engine** sits between the program and everything that looks at it or controls it. Each
Python process runs a **recorder** (live data) and, when debugging, **debugpy** (control). People
connect through their editor (Debug Adapter Protocol, DAP), agents through MCP. Agents work
mostly from recordings (see phase 3); sharing one live debug session with them is for later.

```
  You (VS Code, Cursor, Neovim)      Agents (Claude Code, Cursor, any MCP client)
           │ DAP                              │ MCP
           ▼                                  ▼
     ┌────────────────────── luciole engine ───────────────────────┐
     │  debug session owner · graph · live state · recordings      │
     └───────┬─────────────────────────────────────────┬───────────┘
             │ DAP (control)                            │ live events (data)
             ▼                                          ▼
          debugpy  ───────── same Python process ───── recorder
```

## Phases

| Phase | Theme | Size | Done when |
|---|---|---|---|
| 0 | Consolidate and ship v0.1 | days | a stranger can install it and use it on their own project |
| 1 | Engine foundation | ~1 week | any Python process (terminal, tests, child processes) lights up the same view |
| 2 | Debugging through the engine | weeks | it is used every day on a real project, with breakpoints |
| 3 | History and agents (one track: trace store, MCP, variable history, origins, timeline) | ~2 months | "where did this value come from?" is answered for a real bug, and an agent finds planted bugs faster with Luciole than without |
| 5 | Beyond VS Code | weeks | the same features work from Neovim or a browser |
| 6 | Second language: JS/TS | weeks | the same experience on a TypeScript project |
| 7 | Native: C/C++/Rust | months | live lighting and time travel on a C program |

### Phase 0: Consolidate and ship v0.1

- [x] Commit the current work.
- [x] CI on GitHub Actions: pytest, the extension's unit tests and type check.
- [x] Run configurations: `-m module`, script arguments, pytest, environment variables
  (Run Live…, `luciole.runConfigurations`).
- [x] A wrong caller after deep library stacks was fixed.
- [x] Publish `luciole` on PyPI and the extension on the VS Code Marketplace (v0.1.0, from a tag by
  `.github/workflows/release.yml`). Open VSX (which Cursor, Windsurf and VSCodium install from)
  waits for its token.

### Phase 1: Engine foundation

- [x] The recorder as a library: `luciole.start()` / `luciole.stop()` for code you control
  (servers, notebooks).
- [x] Auto-start: a `sitecustomize.py` starts the recorder in any interpreter whose environment has
  `LUCIOLE_*` set, so terminal runs, test runners and child processes light up unchanged.
  `os.register_at_fork` covers forked children. This is also the multi-process support. Turned on
  and off by a status bar switch in VS Code (new terminals) or `luciole env` in a shell.
- [x] `luciole engine`: a local process that recorders connect to (each labelled with its process
  id and command line) and that viewers subscribe to. The VS Code extension becomes one viewer, and
  starts the engine when none is running.
- [x] A protocol handshake: recorder, engine and viewers state the protocol version they speak.
- [x] Run Live → tests for this file: most files of a library are not meant to run on their own, so
  find the test files that import the current module and run pytest on them (`luciole tests`).

### Phase 2: Debugging through the engine

- [x] Debugging with VS Code's own Python debugger, with the graph: breakpoints, exceptions, watch,
  hover and the debug console are debugpy's; with auto-start on, debug launches get its variables
  so the recorder keeps the graph lit; the graph follows wherever the debugger stops. Done with
  VS Code's API (a debug configuration provider and a debug adapter tracker) rather than a DAP
  proxy, which only phase 3 needs.
- [x] Graph-driven debugging: right-click a neuron to stop the next time that function runs (a
  VS Code breakpoint on its first body line, in the Breakpoints view); neurons holding a
  breakpoint show a red dot. Tested by hand in VS Code on WSL (2026-10-29).
- [x] Better call resolution, with the standard library only (no Pyright or jedi): imports, star
  imports, properties, `super()`, overrides, return types and annotations, special methods,
  functions held in variables, and the project's only method of a name (marked `by_name`). Missing
  runtime calls on `email` + `asyncio`: 52% → 27% (`scripts/missing_calls.py`). What remains needs
  types the code never states (unannotated parameters, `getattr` dispatch, classes built with
  `type()`).
- [x] Callbacks: a function passed as an argument (`Thread(target=f)`, `pool.submit(f)`,
  `key=f`, event handlers) is linked to the function passing it (`"callback": true`).
- [x] Run Live under VS Code's debugger: every run starts through debugpy with the recorder on, so
  live control (breakpoints, stepping, Variables, Call Stack, console) is the debugger's. Decided:
  Luciole's own pause/step buttons and Variables view go; the recorder keeps its value snapshots
  and pause/step protocol for engine clients and for phase 4.
- Scale, a structured layout instead of one force-directed cloud:
  - [x] regions inside regions (packages → modules → classes → functions): nearness means "same
    module";
  - [x] stable positions, seeded from paths: the same project always gives the same map;
  - [x] calls between regions bundled into tracts; region labels, so the overview reads like a map;
  - [x] small graphs (60 functions or fewer) in rows by call order, read like an outline;
  - [x] neurons sized by their connections; search in the sidebar's Map view;
  - focus (the functions N calls around a selection) and filters (tests, private helpers) move to
    phase 3, step 6: the same mechanism as lighting an origin path.
- [x] A minimal look: neutral greys, one accent (the theme's), a muted tint per file; calls trace
  their line from caller to callee; no legend.
- [x] A Luciole sidebar: actions, running programs, a map outline with search.
- [x] Big graphs draw smoothly: spheres and lines in one batch each, nothing drawn while still.

Dropped: semantic zoom (regions opening into modules as you get closer; names already appear as
you zoom in, and the Map view's search flies to any function) and brightness by importance
(against the minimal look; runtime lighting already shows what matters).

Deferred until a need shows up: several clients on one live debug session (a DAP proxy around
debugpy, owned by the engine). Agents debug from recordings (phase 3); a live session matters for
an agent only on a bug that does not reproduce on demand (flaky, a long-running server).

### Phase 3: History and agents (one track)

The "debugger on steroids": VS Code's debugger does live control, Luciole's recorder keeps the
history, and Claude reads it through an MCP server. The goal: go to the origin of anything (a
value, a call, an object, an exception), for you in the view and for Claude through MCP.

The key idea: the static graph already holds every possible data path (`flows_to`, `passes_arg`,
`returns`, reads, writes). The recorder only captures which paths actually happened and with which
values. Together they answer "where does this come from" without tracking every instruction.

**The trace: one per run, in SQLite** (standard library):
- *frames*: id, function (node id), caller frame, thread, start/end time, arguments, return value
  or exception: the **call tree**;
- *writes*: variable (node id), frame, line, time, value: each variable's **history**;
- *values*: capped snapshots (type, short repr, first items of containers), with secret redaction
  (`password`, `token`, `key`…) and size limits per value and per run.

**Levels**, since recording everything is slow; `sys.monitoring` turns detailed events on per
function, which keeps the cost where you look:
- level 0, always on: call tree, timings, exceptions;
- level 1: arguments and return values of project functions;
- level 2, scoped: every assignment in chosen functions (variable history);
- level 3, narrow: instruction-level detail in one function, for exact provenance.

**Agents debug from a recording, not by stepping.** Breakpoints and stepping are made for a person
watching: an agent would have to guess where to stop before knowing what is wrong, pay a tool call
per step, and start over when it stepped past the moment that mattered. Instead, everything an
agent runs is recorded from the first run, and it asks questions afterwards:
- every run starts at level 1 (call tree, arguments, return values, exceptions): a few
  microseconds per call, cheap enough to leave on;
- on a failure (an uncaught exception, a failing test), the locals of every frame on the stack are
  kept, as a post-mortem debugger would show them: free until something fails.

One run then answers most questions: the exception and its chain, the calls that led to it, their
arguments, and the locals at the crash. A second run is needed only when the wrong value was made
long before the failure, in a function whose locals were not kept: the agent re-runs with level 2
on the suspect functions. Most bugs reproduce under a test, which makes that cheap.

**Origins:**
- a value: its last write before that moment and that line; the static edges say which operands
  fed the line, the trace gives their values then; recurse. A dynamic backward slice (the vision's
  "Trace" mode);
- a call: its caller frame and call site, including dynamic calls the static graph never saw;
- an object: where it was created, and every place that changed its attributes;
- an exception: where it was raised, its chain, the values on the way.

**Steps, in order:**

| Step | What | Rough effort |
|---|---|---|
| 1 | Done: trace store, call tree, arguments/returns, exceptions (levels 0–1) | — |
| 2 | Done: `luciole mcp` (docs/mcp.md), trace queries plus graph queries (find, neighbors); its `run` records at level 1 by default | — |
| 3 | Done: failure snapshots, the locals of every frame an exception leaves (level 1), `traceback` | — |
| 4 | Scoped variable history (level 2), redaction, limits; coroutine suspend/resume, so time spent awaiting is not counted as busy | 1–2 weeks |
| 5 | Origin queries: dynamic slicing over static edges and the trace | ~2 weeks |
| 6 | In VS Code: the call tree in the Luciole sidebar, a timeline to scrub (the brain replays), a function's variables and their values over time, origin paths lit on the brain, focus and filters | ~2 weeks |
| 7 | Later: stepping backwards in the debugger over a recording; level 3 provenance | — |

MCP tools (summarized and paginated answers): `run(command, level, scope)`, `call_tree`,
`calls_of(function)` with arguments and returns, `variable_history`, `origin_of(variable, frame,
time)`, `exceptions` (with the failure snapshot), `diff_runs`, and the graph queries. Then a Claude Code plugin (the MCP server,
a skill teaching when to debug with Luciole, slash commands, optional hooks such as recording a
failing test), the agent's activity shown on the graph, explicit approval to run programs, and a
benchmark of planted bugs with and without Luciole.

Effort goes first to the trace store and origin queries (what nothing else offers); the MCP
server early, so Claude is the first user of the data model; the UI after, starting minimal.

### Phase 5: Beyond VS Code

- `luciole serve`: the 3D view in a browser.
- Other editors through DAP (Neovim, Emacs, Zed, JetBrains).
- A `language` field in both formats.
- Decide the engine's implementation language (Python, or Rust/Go) from real trace volumes.

### Phase 6: JS/TS

- js-debug behind the proxy for breakpoints and stepping, plus a recorder built on the Chrome
  DevTools Protocol for live lighting.
- The graph from SCIP indexes or language servers, function-level first.
- Goal: prove the engine design works for more than one language.

### Phase 7: Native languages

- gdb or lldb behind the proxy for debugging; `-finstrument-functions` or XRay for live calls.
- rr integration for time travel.

### Later

Jupyter (`%load_ext luciole`), a pytest plugin, export to the Chrome/Perfetto trace format,
recordings made in CI opened locally, distributed tracing (OpenTelemetry) across services,
aggregates per GPU kernel, sharing recordings with teammates.

## Code health

The codebase must stay easy to change as it grows. Standing rules:

- **Contracts between parts.** The graph format, the live protocol and (soon) the trace schema are
  documented and tested; parts talk only through them (analyzer, recorder, engine, extension, MCP).
  A change to a contract updates its doc and its tests in the same PR.
- **Logic outside the UI.** Anything that can be computed (layout, flow timing, run
  configurations, tree data, queries) lives in modules with no `vscode` or `three` import, with
  unit tests. `main.ts` and `extension.ts` only wire things up.
- **Size limits as a signal.** A file past ~800 lines, or a function past ~80, is split at the next
  feature that touches it, as its own commit.
- **A cleanup PR after each feature cluster**: dead code, duplicated helpers, names that no longer
  fit, docs that drifted. Reviewed like a feature.
- **Every PR reviewed** (code review pass plus the hands-on checklist) before it merges.

## Delivery rules

- The Python core stays standard-library only: it runs inside users' interpreters, next to torch
  and anything else. debugpy is the one exception, for debugging.
- One version number for the Python package and the extension.
- The extension bundles the Python package so it works out of the box, and uses a pip-installed
  `luciole` when present.
- Everything stays on the user's machine; no telemetry by default.

## Open questions

- Whether agents ever need a live debug session (the deferred DAP proxy), or recordings plus
  failure snapshots are enough: the planted-bug benchmark will tell.
