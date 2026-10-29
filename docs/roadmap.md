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
- **Debugger controls in live mode**: Stop, Pause/Resume, Step over/into/out, and a Variables
  view (threads, call stack, expandable values, the stopped line highlighted).
- **The engine** (`luciole engine`) and **auto-start**: any Python process (terminal runs, test
  runners, child processes, debug sessions) lights up the view, with no change to how it starts.
- **Debugging with the graph**: VS Code's Python debugger and the brain together (the stopped
  function glows red, breakpoints from neurons).
- **Call resolution** without a type checker, including callbacks (`Thread(target=f)`): on the
  standard library's `email` and `asyncio`, missing runtime calls went from 52% to 27%.

## Target architecture

One **engine** sits between the program and everything that looks at it or controls it. Each
Python process runs a **recorder** (live data) and, when debugging, **debugpy** (control). People
connect through their editor (Debug Adapter Protocol, DAP), agents through MCP, and both can
share the same session.

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
| 3 | Agents (MCP and Claude Code plugin) | 1–2 weeks | an agent finds a planted bug faster with Luciole than without |
| 4 | Recording and time travel (Python) | weeks to months | "where did this value come from?" is answered for a real bug |
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
- Several clients on one session (needed by phase 3: you and an agent on the same run): a DAP
  proxy around debugpy, owned by the engine.
- [x] Better call resolution, with the standard library only (no Pyright or jedi): imports, star
  imports, properties, `super()`, overrides, return types and annotations, special methods,
  functions held in variables, and the project's only method of a name (marked `by_name`). Missing
  runtime calls on `email` + `asyncio`: 52% → 27% (`scripts/missing_calls.py`). What remains needs
  types the code never states (unannotated parameters, `getattr` dispatch, classes built with
  `type()`).
- [x] Callbacks: a function passed as an argument (`Thread(target=f)`, `pool.submit(f)`,
  `key=f`, event handlers) is linked to the function passing it (`"callback": true`).
- Scale: whole-folder graphs of 30k+ lines that stay readable (search, filtering, modules that
  collapse and expand).
- Decide the future of live mode's own pause/step/Variables view. Partly decided: under VS Code's
  debugger, pausing and stepping are the debugger's (Luciole's buttons hide and drive it); live
  mode's own pause remains for ⚡ Run Live.

### Phase 3: Agents

- A `luciole mcp` server, a client of the engine. Tools: graph queries (neighbors, callers,
  slicing), recording a run, calls per function, comparing two runs, and breakpoints and
  inspection on the shared debug session.
- A Claude Code plugin: the MCP server, a skill that teaches when and how to debug with Luciole,
  and slash commands. Optional hooks, such as recording a failing test run automatically.
- The agent's activity shown on the graph: what it looks at glows, its breakpoints appear.
- Safety: running a program needs explicit approval; answers are summarized and paginated.
- A small benchmark of planted bugs, with and without Luciole. Listing in the MCP registry.

### Phase 4: Recording and time travel (Python)

- Variable-level events: values on function entry and return and on assignments, with size limits
  and redaction of secrets.
- Trace storage (SQLite or a column store), indexed by node and by time.
- A replay timeline in the view: scrub back and forth, with the graph replaying.
- Stepping backwards through recorded runs.
- MCP tools for values, runtime slicing and value-level comparison of runs.

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

## Delivery rules

- The Python core stays standard-library only: it runs inside users' interpreters, next to torch
  and anything else. debugpy is the one exception, for debugging.
- One version number for the Python package and the extension.
- The extension bundles the Python package so it works out of the box, and uses a pip-installed
  `luciole` when present.
- Everything stays on the user's machine; no telemetry by default.

## Open questions

- Order of phases 2 and 3: MCP graph and run tools could ship before the DAP proxy, since the
  engine already serves them.
- When to publish: phase 0, or after phase 2.
