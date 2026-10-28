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
- Build and release: `uv build`, `uv publish`, tag `vX.Y.Z`.
- Extension (`extension/`, npm): `npm install`, `npm run build`, `npm test` (view-model unit tests), `npx tsc --noEmit`.
  `npm run install-local` packages `luciole.vsix` (with a copy of the analyzer in `extension/python/`) and installs it in VS Code; reload the window afterwards. Re-run it after changing the extension or the analyzer.
  Or press F5 ("Run Luciole extension") to open a separate dev window on this repo, which uses `../src` live.
  The extension runs the analyzer as `python -m luciole` with `PYTHONPATH` pointing at `../src` (stdlib only, any Python 3.10+).
- Webview without VS Code: `npm run screenshot -- [path] [--events FILE]` renders the real webview bundle in headless Chromium (software WebGL) and saves PNGs to `extension/out/dev/shots/`; `--events` replays a `luciole run --events FILE` recording. On this WSL setup Chromium needs `libasound.so.2`: install `libasound2t64`, or point `LD_LIBRARY_PATH` at an extracted copy.

## Roadmap

Full plan: `docs/roadmap.md` (phases 0–7: ship v0.1, engine foundation (auto-start recorder, local engine that recorders and viewers connect to), debugging through a DAP proxy around debugpy, agents (MCP + Claude Code plugin), recording and time travel, beyond VS Code, JS/TS, native languages).

Done so far:

- **Graph schema**: `docs/graph-format.md`.
- **Static parser**: `luciole graph <path> [--json] [-o FILE]`, in `src/luciole/analyzer.py` (rules in `docs/graph-format.md`).
- **Extension**: `extension/` (see Development setup), a 3D brain. The editor title button graphs the current file and follows the active editor; "Show Graph (Whole Folder)" from the palette or Explorer.
- **Live mode**: `luciole run` + "Luciole: Run Live" (⚡ button) light up running functions, the current call path and pulses along synapses; calls missed by static analysis appear as runtime synapses. Threads (one stack per named thread, `Thread.start` spawn links) and asyncio (waiting coroutines in blue, task spawn links from where the coroutine was created) are covered; `examples/workers.py` exercises both. The recorder must never read other threads' frames (`sys._current_frames()` breaks `torch.export`): all state is kept by the monitoring callbacks in-thread.
- **Live debugger controls**: Pause/Resume and Step over/into/out (F10/F11/Shift+F11) with a Variables view (cooperative: LINE events, each thread snapshots its own frames, commands over the live socket, `src/luciole/values.py`, `extension/src/variables.ts`); Stop (SIGINT to the run's process, then SIGKILL; `hide_recorder_frames` keeps the traceback ending in project code).

## Prior art (for reference, not to copy)

- Crabviz, CoGraph — VS Code call-graph extensions (function level only, no variables, no runtime).
- Sourcetrail — interactive code explorer (discontinued).
- Python Tutor, birdseye — runtime variable visualization, small programs only.
- VS Code Debug Visualizer — visualizes data during debugging.

Luciole's distinguishing features: variable-level flow plus runtime lighting on a full-project graph.

## Where to start

Next work follows `docs/roadmap.md`, starting with phase 0 (commit, CI, run configurations, first release).