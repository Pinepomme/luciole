# Luciole

Luciole turns a Python project into an explorable graph inside VS Code, and lights it up as the code runs.
The goal is to make large codebases (tens of thousands of lines) understandable at a glance.

Repo: https://github.com/Pinepomme/luciole — MIT license — PyPI name: `luciole`

## Vision

- **One block per function.** The overview shows every function as a node, grouped and collapsible by module and class, with call edges between them.
- **One line per variable.** Opening a function block shows its variables and how values flow: arguments in, intermediate variables, return value out. Variables can also be traced across functions (passed as arguments, returned, stored on `self`).
- **Runtime lighting.** While the program runs, the graph lights up: the active function glows, variables flash when written or read, edges pulse as values move between functions.

The name: *luciole* is French for "firefly" — little lights blinking across a dark network.

## Key design constraints

- **Scale.** Drawing every variable edge of a 30k-line project at once is an unreadable hairball. The UI must rely on scoping and progressive disclosure:
  1. Overview: functions and calls, collapsed by module/class.
  2. Focus: expand one function to see its internal variable flow.
  3. Trace: select one variable and highlight only its path across functions (program slicing).
- **Runtime speed.** Real code runs far too fast to watch live. Support two modes:
  - Debugger mode: follow VS Code's debugger (Debug Adapter Protocol) step by step.
  - Record & replay: record events at full speed, then scrub through a timeline in the graph.
- **Static vs runtime mismatch.** Dynamic calls (`getattr`, callbacks, monkey-patching) produce runtime events with no static edge. Show these as "discovered at runtime" edges rather than dropping them.

## Architecture

Two halves that communicate **only through a JSON graph format**:

1. **Python analyzer** (`src/luciole/`) — parses a project and emits the graph JSON. Later also records runtime events.
2. **VS Code extension** (`extension/`, TypeScript) — runs the analyzer and renders the graph in a webview as a 3D "brain": one neuron per function, synapses = the variables flowing between functions (arguments and return values along calls, plus shared state: globals and attributes written by one function and read by another). Live mode runs a program under the recorder and lights up the functions as they run.

Defining the JSON schema first lets both halves be built and tested independently.

### Graph schema

Spec: `docs/graph-format.md`. JSON Schema: `schema/graph.schema.json`. Example: `examples/shop.py` → `examples/shop.graph.json` (validated by `tests/test_graph_format.py`).

- Nodes: `module`, `class`, `function`, `variable` — each with id (stable qualified name, e.g. `pkg.mod:Class.method#var`), name, parent id, file, loc. Each function's return value is a `variable` named `return`.
- Containment is the `parent` field only (no `contains`/`defines` edges).
- Edges: `calls`, `reads`, `writes`, `flows_to`, `passes_arg`, `returns` — one per (kind, source, target), with a `sites` list; `provenance: "runtime"` marks edges only seen at runtime.
- Live events: `luciole run` streams JSON Lines frames (call counts, caller>callee counts, sampled stacks) by node id; see "Live events" in `docs/graph-format.md`.

## Tech stack

- Parsing: Python's built-in `ast` (tree-sitter possible later for other languages).
- Graph model: plain dicts for now; `networkx` if graph algorithms (slicing, layout prep) need it.
- Call resolution (later): Pyright or `jedi`, much better than raw AST matching for methods, imports, aliases.
- Runtime recording: `sys.monitoring` (Python 3.12+), low overhead, can be enabled per function.
- Rendering: VS Code webview with `3d-force-graph` (three.js). Brain model in `extension/src/webview/brain.ts`, rendering and live lighting in `extension/src/webview/main.ts`, live session (terminal + local socket) in `extension/src/live.ts`.

## Development setup

- WSL, repo at `~/dev/luciole`.
- Python environment managed with **uv**: `uv sync`, `uv run ...`, `uv add <pkg>`, `uv add --dev <pkg>`.
- `src/` layout, version in `src/luciole/__init__.py` (`__version__`), read dynamically by hatchling.
- Tests with `pytest` (`uv run pytest`).
- CI: `.github/workflows/ci.yml` (pytest on 3.10–3.14 and on macOS/Windows with 3.12; extension typecheck, tests, packaging).
- Release (one version for both halves): set it in `src/luciole/__init__.py` and `extension/package.json`, add a `## X.Y.Z` section to `CHANGELOG.md` on `dev`, open a PR from `dev` into `main` and merge it, then tag `vX.Y.Z` on `main` and push the tag (the workflow refuses tags that are not on `main`). `.github/workflows/release.yml` checks the versions, runs the tests, builds once and publishes to PyPI (trusted publishing, environment `pypi`), the VS Code Marketplace (publisher `pinepomme`, secret `VSCE_PAT`, an Azure DevOps token that expires within a year) and Open VSX (namespace `pinepomme`, secret `OVSX_PAT`), both in environment `release`, then creates the GitHub release. A failed publish job can be re-run on its own; a registry whose secret is not set is skipped. The extension is marked `"preview": true` until 1.0.
- Extension (`extension/`, npm): `npm install`, `npm run build`, `npm test` (view-model unit tests), `npx tsc --noEmit`.
  `npm run install-local` packages `luciole.vsix` (with a copy of the analyzer in `extension/python/`) and installs it in VS Code; reload the window afterwards. Re-run it after changing the extension or the analyzer.
  Or press F5 ("Run Luciole extension") to open a separate dev window on this repo, which uses `../src` live.
  The extension runs the analyzer as `python -m luciole` with `PYTHONPATH` pointing at `../src` (stdlib only, any Python 3.10+).
- Webview without VS Code: `npm run screenshot -- [path] [--events FILE]` renders the real webview bundle in headless Chromium (software WebGL) and saves PNGs to `extension/out/dev/shots/`; `--events` replays a `luciole run --events FILE` recording. On this WSL setup Chromium needs `libasound.so.2`: install `libasound2t64`, or point `LD_LIBRARY_PATH` at an extracted copy.

## Roadmap and status

Detailed plan: `docs/roadmap.md`. This section is the summary a new session needs to pick up the work.

### Done

- **Graph schema**: `docs/graph-format.md`.
- **Static parser**: `luciole graph <path> [--json] [-o FILE]`, in `src/luciole/analyzer.py` (rules in `docs/graph-format.md`).
- **Extension**: `extension/` (see Development setup), a 3D brain. The editor title button graphs the current file and follows the active editor; "Show Graph (Whole Folder)" from the palette or Explorer.
- **Live mode**: `luciole run` + "Luciole: Run Live" (⚡ button) light up running functions, the current call path and pulses along synapses; calls missed by static analysis appear as runtime synapses. Threads (one stack per named thread, `Thread.start` spawn links) and asyncio (waiting coroutines in blue, task spawn links from where the coroutine was created) are covered; `examples/workers.py` exercises both. The recorder must never read other threads' frames (`sys._current_frames()` breaks `torch.export`): all state is kept by the monitoring callbacks in-thread. When no project caller is within 40 frames, the recorder walks the whole stack (never guesses from its own stack).
- **Run Live picks how to run a file** (`fileRunKind` in `extension/src/runs.ts`): test files with pytest, scripts (`if __name__ == "__main__":`) as themselves, other modules through the test files that import them (`luciole tests FILE`, `src/luciole/testfinder.py`), else as a script.
- **Run Live…** (`extension/src/runs.ts`): current file with arguments, a module (`pytest -x tests`), or `luciole.runConfigurations` from settings; the panel's Run live button repeats the last run.
- **Live debugger controls**: Pause/Resume and Step over/into/out (F10/F11/Shift+F11) with a Variables view (cooperative: LINE events, each thread snapshots its own frames, commands over the live socket, `src/luciole/values.py`, `extension/src/variables.ts`); Stop (SIGINT to the run's process, then SIGKILL; `hide_recorder_frames` keeps the traceback ending in project code).
- **Phase 0 (ship v0.1)**: v0.1.0 is published on PyPI and the VS Code Marketplace (2026-10-28, by the release workflow); Open VSX is skipped until `OVSX_PAT` is set.
- **Debugging with the graph** (phase 2): `extension/src/debugging.ts` uses VS Code's API, not a DAP proxy: a debug configuration provider for `debugpy`/`python` adds the auto-start variables to launches when the switch is on; a debug adapter tracker sees `stopped` events and asks for the top frame, and the brain glows that function red and flies to it (`neuronAt` in `brain.ts`); right-click a neuron toggles a VS Code breakpoint at the function's `body_line` (new field: first statement after the docstring), and neurons holding a breakpoint get a red dot. `tests/test_debugpy.py` drives debugpy over DAP while auto-start records (debugpy is a dev dependency). `npm run screenshot -- FILE --breakpoint F:L --stopped F:L` renders it.
- **Engine** (phase 1): `luciole engine` (`src/luciole/engine.py`, protocol in `src/luciole/protocol.py`): recorders and viewers connect to it with a hello (`protocol: 1`, refused otherwise); it numbers recorders (`process`/`from`/`exit` to viewers, `to` on commands), shows each viewer the recorders under its roots, resumes paused programs when the last viewer leaves, writes `~/.luciole/engine-v1.json` (`LUCIOLE_HOME` in tests, set by `tests/conftest.py`), exits after 10 idle minutes. The extension (`extension/src/engine.ts`) starts it detached when none answers and subscribes as a viewer (`LiveSession`); the auto-start switch no longer sets a port.
- **Auto-start** (phase 1, step 1): `src/luciole/_autostart/sitecustomize.py` starts the recorder in any process with that folder on PYTHONPATH when the engine answers (`LUCIOLE_LIVE_PORT`, else its file; nothing at all otherwise); it runs the sitecustomize it hides, drops the empty PYTHONPATH entry VS Code's prepend can leave, and skips `luciole run`. `recorder.Session` (start/stop), `luciole.start()`/`stop()`, forked children reconnect (`os.register_at_fork`, `os._exit` flushes), tool id 4/3 (never DEBUGGER/COVERAGE/PROFILER). `luciole env [--off]` for shells. Extension: status bar switch `luciole.autoStart` sets the variables in new terminals (`environmentVariableCollection`); `LiveSession` shows many processes, `extension/src/processes.ts` merges their stacks and keeps thread ids/refs apart; Run Live is the connection whose pid is its terminal's.

### Next steps, in order

1. **Hands-on test of the extension in a real VS Code window** (never done; only the Python side and headless rendering were tested): `examples/workers.py` with ⚡ Run Live, Pause / F10 / F5 / Stop, the Variables view; the auto-start switch with `pytest` in a new terminal. Fix what breaks.
2. **Phase 2 (rest)**: better call resolution (calls through imports, class bodies, `@property` and methods on objects of unknown type are missing statically; Pyright/jedi, or more AST rules), and scale (whole-folder graphs of 30k+ lines are an unreadable cloud today: search, filtering, collapsible modules). Decide the future of live mode's own pause/step view now that the real debugger works with the graph. Several clients on one debug session (a DAP proxy around debugpy, owned by the engine) moves to phase 3, which needs it.
3. **Phase 3: agents**: `luciole mcp` server (graph queries, record a run, calls per function, compare runs, breakpoints/inspection on the shared session), a Claude Code plugin (MCP + a skill teaching when to debug with Luciole + slash commands, optional hooks), the agent's activity shown on the graph, explicit approval to run programs, summarized/paginated answers, a small planted-bug benchmark.
4. **Phase 4: recording and time travel** (Python): variable-level events with size limits and secret redaction, trace storage (SQLite or column store), a replay timeline, stepping backwards, MCP tools for values/slicing/run diffs.
5. **Phase 5: beyond VS Code**: `luciole serve` (3D view in a browser), other editors through DAP, a `language` field in both formats, decide the engine's implementation language.
6. **Phase 6: JS/TS** (js-debug behind the proxy, Chrome DevTools Protocol recorder, graph from SCIP/language servers) and **phase 7: native** (gdb/lldb behind the proxy, `-finstrument-functions` or XRay, rr for time travel).

Open question: phases 2 and 3 could swap (MCP graph and run tools are cheap once the engine exists).

### Decisions and conventions

- Personal project of the user (not tied to their employer), MIT. Their work email must never appear in commits: commit as `Pinepomme <45495853+Pinepomme@users.noreply.github.com>` (set in this repo's git config). History was rewritten once to remove it.
- Git pushes go to `origin` (`git@github.com:Pinepomme/luciole.git`), whose SSH key is Pinepomme's on this WSL setup (an SSH alias `github-luciole` may exist on other machines). The `gh` CLI has two accounts: the active one is the user's work account `alexandresuescun`, which must not be used for this repo. For PRs: `gh auth switch --hostname github.com --user Pinepomme`, then switch back with `--user alexandresuescun`.
- Branches: `main` is what was released (tags `vX.Y.Z` are on it), `dev` is where work lands. Work on a feature branch from `dev` and open a PR into `dev`; a release is a PR from `dev` into `main`. CI runs on PRs and on pushes to `main` and `dev`.
- Delivery rules: the Python core stays standard-library only (debugpy is the one exception, for debugging); one version number for both halves; the extension bundles the analyzer; no telemetry; everything stays on the user's machine.
- Branding is postponed.

## Prior art (for reference, not to copy)

- Crabviz, CoGraph — VS Code call-graph extensions (function level only, no variables, no runtime).
- Sourcetrail — interactive code explorer (discontinued).
- Python Tutor, birdseye — runtime variable visualization, small programs only.
- VS Code Debug Visualizer — visualizes data during debugging.

Luciole's distinguishing features: variable-level flow plus runtime lighting on a full-project graph.

## Where to start

Follow "Next steps, in order" above, starting with the hands-on test of the extension, then phase 2.
