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
2. **VS Code extension** (`extension/`, TypeScript) — runs the analyzer and renders the graph in a webview.

Defining the JSON schema first lets both halves be built and tested independently.

### Graph schema (to design first)

- Nodes: `module`, `class`, `function`, `variable` — each with id, name, file path, start/end line, parent id.
- Edges: `contains`, `calls`, `defines`, `reads`, `writes`, `passes_arg`, `returns`.
- Runtime events (later): timestamped `call`, `return`, `read`, `write` referencing node ids.

## Tech stack

- Parsing: Python's built-in `ast` (tree-sitter possible later for other languages).
- Graph model: `networkx`.
- Call resolution (later): Pyright or `jedi`, much better than raw AST matching for methods, imports, aliases.
- Runtime recording: `sys.monitoring` (Python 3.12+), low overhead, can be enabled per function.
- Rendering: VS Code webview with Cytoscape.js and the ELK layout (handles nested blocks well).

## Development setup

- WSL, repo at `~/dev/luciole`.
- Python environment managed with **uv**: `uv sync`, `uv run ...`, `uv add <pkg>`, `uv add --dev <pkg>`.
- `src/` layout, version in `src/luciole/__init__.py` (`__version__`), read dynamically by hatchling.
- Tests with `pytest` (`uv run pytest`).
- Build and release: `uv build`, `uv publish`, tag `vX.Y.Z`.

## Roadmap

1. **Graph schema** — write the schema plus a hand-made example JSON for a tiny file.
2. **Static parser (v0.1)** — `luciole graph <path> --json`: functions, calls between them, variable assignments and uses inside each function. Test on a small file, then a real project.
3. **Extension webview** — "Luciole: Show Graph" command: run the parser, render with Cytoscape.js + ELK, collapsible modules, click a node to jump to the code.
4. **Better resolution** — Pyright/jedi for accurate calls; cross-function variable tracing.
5. **Runtime lighting** — `sys.monitoring` recorder writing events to a file, replay timeline in the webview; then live debugger mode.

First milestone: steps 1–3 working on a single file, to validate that the visual approach actually clarifies code.

## Prior art (for reference, not to copy)

- Crabviz, CoGraph — VS Code call-graph extensions (function level only, no variables, no runtime).
- Sourcetrail — interactive code explorer (discontinued).
- Python Tutor, birdseye — runtime variable visualization, small programs only.
- VS Code Debug Visualizer — visualizes data during debugging.

Luciole's distinguishing features: variable-level flow plus runtime lighting on a full-project graph.

## Where to start

Begin with roadmap step 1: propose the JSON schema, write an example for a small sample file, then implement the `ast`-based parser with tests.