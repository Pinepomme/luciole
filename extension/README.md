# Luciole

See a Python project as a 3D brain: one glowing neuron per function, and lines between them for
the variables that flow from one function to another (arguments in, return values out, and
globals or attributes one function writes and another reads). Run your program live and watch
the functions light up as they run.

## Use

- **Run Live** (⚡ in the title bar of a Python editor) runs the file in a terminal and lights up
  the brain: functions on the current call path glow, functions that just ran flash and fade,
  pulses travel along the lines as calls happen. The bar at the bottom shows what is running now.
  Functions that ran stay softly lit afterwards. Calls the static analysis missed appear as
  orange lines.
- **Auto-start** (**Luciole: off** / **on** in the status bar): when on, every Python program
  you start in a new VS Code terminal lights up the brain, run the way you always run it: a
  script, `pytest`, a server, and the processes they start. Nothing changes in how programs run,
  and with the switch off nothing is set at all. Terminals opened before the switch keep their old
  setting: open a new one.
- **Stop** (in the panel's toolbar, or ⏹ in the title bar in place of ⚡ while a run is going)
  interrupts the program like Ctrl+C: it gets a `KeyboardInterrupt`, its `finally` blocks run, and
  the traceback in the terminal shows where it was. If it keeps running (it catches the interrupt,
  or threads keep it alive), press **Force stop** to kill it.
- **Pause** stops every thread at its next line of your code, and **Resume** lets them go on.
  The **Variables** view (Luciole icon in the activity bar) then shows, like VS Code's debugger,
  each stopped thread, the functions on its call path, and their variables: expand objects, lists
  and dicts to look inside, click a function to see its line and its neuron. The lines where
  threads stopped are highlighted in the editor. Also in the title bar (⏸ / ▶) and the command
  palette. A thread busy in a library or in C code (a sleep, a long torch call) stops when it
  gets back to your code. With several processes lit up, Pause and Stop apply to all of them.
- **Step over / into / out** (F10 / F11 / Shift+F11 while paused, or the buttons) run the thread
  you are looking at to its next line in the same function (calls on that line run without
  stopping), to its very next line (inside calls too), or until its function returns. On an
  `await`, a step over stops when the coroutine goes on. The other threads run during the step,
  then everything stops again. F5 resumes. What you expanded in the Variables view stays expanded
  from one step to the next.
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
button, or the editor title bar's menu): the current file with arguments, a module such as
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
`.venv` if there is one, then `python3`. Set `luciole.pythonPath` to choose another interpreter.
Nothing needs installing in your project.
