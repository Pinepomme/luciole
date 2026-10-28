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
- Release (one version for both halves): set it in `src/luciole/__init__.py` and `extension/package.json`, add a `## X.Y.Z` section to `CHANGELOG.md`, merge, then tag `vX.Y.Z` on `main` and push the tag. `.github/workflows/release.yml` checks the versions, runs the tests, builds once and publishes to PyPI (trusted publishing, environment `pypi`), the VS Code Marketplace (publisher `pinepomme`, secret `VSCE_PAT`, an Azure DevOps token that expires within a year) and Open VSX (namespace `pinepomme`, secret `OVSX_PAT`), both in environment `release`, then creates the GitHub release. A failed publish job can be re-run on its own; a registry whose secret is not set is skipped. The extension is marked `"preview": true` until 1.0.
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
- **Run Live…** (`extension/src/runs.ts`): current file with arguments, a module (`pytest -x tests`), or `luciole.runConfigurations` from settings; the panel's Run live button repeats the last run.
- **Live debugger controls**: Pause/Resume and Step over/into/out (F10/F11/Shift+F11) with a Variables view (cooperative: LINE events, each thread snapshots its own frames, commands over the live socket, `src/luciole/values.py`, `extension/src/variables.ts`); Stop (SIGINT to the run's process, then SIGKILL; `hide_recorder_frames` keeps the traceback ending in project code).
- **Phase 0 (ship v0.1)**: CI, version 0.1.0 for both halves, README, CHANGELOG, Marketplace icon, release steps above. PR #1 (`phase-0` into `main`) is merged; CI passed on Linux 3.10–3.14 and Windows, macOS was still queued at the last check.

### Next steps, in order

1. **Hands-on test of the extension in a real VS Code window** (never done; only the Python side and headless rendering were tested): `examples/workers.py` with ⚡ Run Live, Pause / F10 / F5 / Stop, the Variables view. Fix what breaks. Check macOS CI on PR #1 / main.
2. **Publish v0.1.0** (needs the user's accounts and tokens; nothing published yet): VS Code Marketplace (publisher `pinepomme`, to create), Open VSX (namespace `pinepomme`, free), PyPI (`luciole`, free). Steps in Development setup.
3. **Phase 1: engine foundation** (~1 week):
   - Recorder as a library: `luciole.start()`.
   - Auto-start: a `sitecustomize.py` starts the recorder in any interpreter whose environment has `LUCIOLE_*` set (terminal runs, test runners, debug sessions, child processes); `os.register_at_fork` for forked children. This is also the multi-process support.
   - `luciole engine`: a local process recorders connect to (labelled with pid and command line) and viewers subscribe to; the VS Code extension becomes one viewer and starts the engine when none runs.
   - Protocol handshake: recorder, engine and viewers state the protocol version they speak.
   - Run Live → tests for this file: find test files that import the current module and run pytest on them (library files are not meant to run on their own).
4. **Phase 2: debugging through the engine**: a DAP proxy around debugpy registered as a VS Code debug type ("Debug Live"); breakpoints, exceptions, watch, hover and console come from debugpy (VS Code's own gutter breakpoints flow through the proxy: merge VS Code's per-file breakpoint list with Luciole's own, and create graph-driven breakpoints through the VS Code API so they show in the Breakpoints panel); the recorder keeps the graph lit; click a neuron to stop the next time that function runs; several clients on one session. Also: better call resolution (Pyright/jedi), and scale (whole-folder graphs of 30k+ lines are an unreadable cloud today: search, filtering, collapsible modules). Decide the future of live mode's own pause/step view.
5. **Phase 3: agents**: `luciole mcp` server (graph queries, record a run, calls per function, compare runs, breakpoints/inspection on the shared session), a Claude Code plugin (MCP + a skill teaching when to debug with Luciole + slash commands, optional hooks), the agent's activity shown on the graph, explicit approval to run programs, summarized/paginated answers, a small planted-bug benchmark.
6. **Phase 4: recording and time travel** (Python): variable-level events with size limits and secret redaction, trace storage (SQLite or column store), a replay timeline, stepping backwards, MCP tools for values/slicing/run diffs.
7. **Phase 5: beyond VS Code**: `luciole serve` (3D view in a browser), other editors through DAP, a `language` field in both formats, decide the engine's implementation language.
8. **Phase 6: JS/TS** (js-debug behind the proxy, Chrome DevTools Protocol recorder, graph from SCIP/language servers) and **phase 7: native** (gdb/lldb behind the proxy, `-finstrument-functions` or XRay, rr for time travel).

Open question: phases 2 and 3 could swap (MCP graph and run tools are cheap once the engine exists).

### Decisions and conventions

- Personal project of the user (not tied to their employer), MIT. Their work email must never appear in commits: commit as `Pinepomme <45495853+Pinepomme@users.noreply.github.com>` (set in this repo's git config). History was rewritten once to remove it.
- Git pushes go to `origin` (`git@github.com:Pinepomme/luciole.git`), whose SSH key is Pinepomme's on this WSL setup (an SSH alias `github-luciole` may exist on other machines). The `gh` CLI has two accounts: the active one is the user's work account `alexandresuescun`, which must not be used for this repo. For PRs: `gh auth switch --hostname github.com --user Pinepomme`, then switch back with `--user alexandresuescun`.
- Work on a branch and open a PR into `main`; CI runs on PRs and pushes to `main`.
- Delivery rules: the Python core stays standard-library only (debugpy is the one exception, for debugging); one version number for both halves; the extension bundles the analyzer; no telemetry; everything stays on the user's machine.
- Branding is postponed.

## Prior art (for reference, not to copy)

- Crabviz, CoGraph — VS Code call-graph extensions (function level only, no variables, no runtime).
- Sourcetrail — interactive code explorer (discontinued).
- Python Tutor, birdseye — runtime variable visualization, small programs only.
- VS Code Debug Visualizer — visualizes data during debugging.

Luciole's distinguishing features: variable-level flow plus runtime lighting on a full-project graph.

## Where to start

Follow "Next steps, in order" above, starting with the hands-on test of the extension, then publishing (ask the user for their accounts), then phase 1.
