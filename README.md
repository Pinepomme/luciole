# Luciole

Luciole turns a Python project into a graph you can explore, and lights it up as the code runs.
*Luciole* is French for "firefly".

- **One neuron per function.** The lines between them are the variables that flow from one
  function to another: arguments in, return values out, and globals or attributes one function
  writes and another reads.
- **Live mode.** Run a program, a module or your tests under the recorder: running functions glow,
  pulses travel along calls, and every thread and asyncio task shows its call path.
- **Debugger controls.** Pause, step over/into/out, and look at every function's variables.

Luciole has two parts: this Python package (the analyzer and the live recorder), and a VS Code
extension that shows the graph in 3D. The extension bundles the package, so in VS Code there is
nothing to install in your project.

## Install

```sh
pip install luciole        # or: uv tool install luciole
```

The package has no dependencies. The graph needs Python 3.10 or newer, live mode 3.12 or newer.

## Use

```sh
luciole graph src/                    # summary of the graph
luciole graph src/ --json -o graph.json
luciole run train.py --epochs 1       # run a script under the recorder
luciole run --events run.jsonl -m pytest -q
```

`luciole run` reports which project functions run, and who calls whom, as JSON lines: to the VS
Code extension, or to a file with `--events`. Only code under `--root` (default: the current
directory) is reported.

Without changing how a program is started, any Python process can light up the view too:

```sh
eval "$(luciole env)"                 # this shell's Python programs light up the view
eval "$(luciole env --off)"           # back to normal
```

They report to the Luciole engine, a small local process that the VS Code extension starts
(or `luciole engine`); when it doesn't run, nothing happens.

```python
import luciole
luciole.start()                       # from inside a program: a server, a notebook
```

In VS Code, the **Luciole: on/off** switch in the status bar does the same for new terminals.

The graph format and the live events are documented in
[docs/graph-format.md](docs/graph-format.md), and the plan in [docs/roadmap.md](docs/roadmap.md).

## Status

Early: version 0.1. It works on real projects of tens of thousands of lines, but large graphs are
still dense, and calls through objects whose type the analyzer can't infer show up only at runtime.

## License

MIT
