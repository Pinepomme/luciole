# Luciole

See a Python project as a 3D brain: one glowing neuron per function, and lines between them for
the variables that flow from one function to another (arguments in, return values out, and
globals or attributes one function writes and another reads). Run your program live and watch
the functions light up as they run.

## Use

- **Run Live** (⚡ in the title bar of a Python editor) runs the file under VS Code's Python
  debugger and lights up the brain. A test file runs with pytest; a script (it has
  `if __name__ == "__main__":`) runs as itself; any other module, which is not meant to run on its
  own, runs through the test files that import it. In the brain: functions on the current call path
  glow, functions that just ran flash and fade, pulses travel along the lines as calls happen. The
  bar at the bottom shows what is running now. Functions that ran stay softly lit afterwards.
  Calls the static analysis missed appear as orange lines.
- Since the run is an ordinary debug session, everything of VS Code's debugger works on it:
  breakpoints, stopping on exceptions, Pause, Step over / into / out (F10 / F11 / Shift+F11),
  the Variables, Watch and Call Stack views, hovering a variable, the Debug Console. The program's
  output and input are in the terminal. Run Live needs the **Python Debugger** extension
  (`ms-python.debugpy`, installed with the Python extension); without it, the program runs in a
  plain terminal and only lights up the brain.
- **Auto-start** (**Luciole: off** / **on** in the status bar): when on, every Python program
  you start in a new VS Code terminal lights up the brain, run the way you always run it: a
  script, `pytest`, a server, and the processes they start. Nothing changes in how programs run,
  and with the switch off nothing is set at all. Terminals opened before the switch keep their old
  setting: open a new one.
- **Stop**: a run under the debugger is stopped from VS Code's debug toolbar, which also pauses and
  steps it (the panel shows no buttons of its own then). A run in a plain terminal (no Python
  Debugger extension, or auto-start) has a Stop in the panel, which interrupts it like Ctrl+C: it gets a
  `KeyboardInterrupt`, its `finally` blocks run, and the traceback in the terminal shows where it
  was. If it keeps running (it catches the interrupt, or threads keep it alive), press
  **Force stop** to kill it.
- **Debugging**: the brain follows VS Code's Python debugger, in a Run Live or when you debug the
  usual way (F5): when the debugger stops, its function glows red and the view flies to it. With
  auto-start on, programs you debug from your own launch configurations light up too.
  **Right-click a neuron** to stop
  each time that function is called (a normal breakpoint, on its first line, listed in the
  Breakpoints view); functions with a breakpoint on any of their lines have a red dot, and
  right-clicking them again removes those breakpoints.
- Threads and asyncio: each thread gets its own line in the bottom bar. Coroutines paused on an
  `await` glow blue, so you can tell running work (amber) from work waiting on I/O. Teal lines
  show where a thread or task was started from. Try it on `examples/workers.py`.
- The graph button graphs the current file and follows you between Python files. To graph a
  whole project, right-click its folder in the Explorer, or run
  **Luciole: Show Graph (Whole Folder)**.
- Drag to rotate, scroll to zoom. Hover a neuron or a line to see what it is. Click a neuron to
  jump to its code.

The graph refreshes when you save a Python file.

To run something other than the current file, use **Luciole: Run Live…** (the panel's **Run…**
button, or the editor title bar's menu): the current file as a script or with arguments, the tests
for the current file, a module such as
`pytest -x tests`, or one of your run configurations. The panel's **Run live** button repeats the
last run. Run configurations go in `settings.json`:

```json
"luciole.runConfigurations": [
  { "name": "train (small)", "program": "scripts/train.py", "args": ["--config", "configs/small.yaml"] },
  { "name": "fast tests", "module": "pytest", "args": ["-q", "-m", "not slow"], "env": { "SEED": "0" } }
]
```

`program` is relative to the workspace folder; `module` runs with `python -m`; `args`, `cwd` and
`env` are optional; `${workspaceFolder}` and `${file}` are replaced.

## Requirements

Python 3.10 or newer for the graph, 3.12 or newer for live mode. Luciole uses the workspace's
`.venv` if there is one, then `python3` (Run Live: the interpreter chosen in the Python
extension). Set `luciole.pythonPath` to choose another interpreter. For Run Live under the
debugger, the Python Debugger extension (`ms-python.debugpy`). Nothing needs installing in your
project.
