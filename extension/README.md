# Luciole

See a Python project as a 3D brain: one neuron per function, and lines between them for
the variables that flow from one function to another (arguments in, return values out, and
globals or attributes one function writes and another reads). Run your program live and watch
the functions light up as they run.

## Use

- **Run Live** (⚡ in the title bar of a Python editor) runs the file under VS Code's Python
  debugger and lights up the brain. A test file runs with pytest; a script (it has
  `if __name__ == "__main__":`) runs as itself; any other module, which is not meant to run on its
  own, runs through the test files that import it. In the brain, activity shows in one accent
  color, your theme's: functions on the current call path are filled with it and ringed, functions
  that just ran fade back to grey, and each call traces its line from the caller to the callee.
  The bar at the bottom shows what is running now. Calls the static analysis missed appear as new
  lines.
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
  `await` get a ring but no fill, so you can tell running work from work waiting on I/O. New
  lines show where a thread or task was started from (hover one to see). Try it on `examples/workers.py`.
- The graph button graphs the current file and follows you between Python files. To graph a
  whole project, right-click its folder in the Explorer, or run
  **Luciole: Show Graph (Whole Folder)**.
- Drag to rotate, scroll to zoom. Hover a neuron or a line to see what it is. Click a neuron to
  jump to its code.
- **History**: every Run Live is also recorded (level 1: each call of your project's code with its
  arguments and return value, and every exception), so you can go back to what the program did
  after it ran. The **Calls** view lists the runs of the workspace, newest first, with their
  command, time and exit status: yours, and those an agent ran with `luciole mcp` (marked
  "agent"). Under a run: its processes (or, with one, its calls directly), each with its
  **Exceptions** (the frames each went through, and their local variables as the exception left
  them) and its **call tree**, opened a level at a time, repeated calls collapsed into `×N`. Each
  call shows its arguments and what it returned (`→ 18.0`) or raised (`✕ ValueError: …`, with an
  error icon). Click a call: the brain flies to its function and rings it, and the editor opens
  at the line that called it. **Show Values** (on a call) opens its arguments, return value or
  exception, the locals an exception left, and, when the run recorded that function line by line
  (`luciole run --scope`, or an agent's `run` with `scope`), each variable's values over time,
  with links to the lines. Turn recording off with the `luciole.recordHistory` setting. It needs
  Python 3.12+ in the program; with an older one, the run is listed as "nothing recorded".
- **Replay**: the ▷ button on a run (or a process, or the panel's **Replay** button for the last
  run) replays it on the brain as it ran live: functions light up, calls trace their lines, the
  bottom bar shows the call path. A timeline under the brain plays and pauses it, the scrubber
  jumps anywhere in the run, and the speed (1/10000× to 10×) slows down runs too fast to see;
  it starts at a speed that makes the replay last a few seconds. Only what is on screen is read
  from the recording, so long runs replay as fast as short ones. Coroutines waiting on an
  `await` are not ringed in a replay (a recording has their total waiting time, not when).
- **Where did this value come from?** Right-click a variable in a Python file (the name under
  the cursor, or selected: a local, a parameter, a global, `self.x`, or `return`) and pick
  **Luciole: Where Did This Value Come From?**: Luciole finds the function around the cursor, the
  newest recorded run that called it, and asks about its last call (or lets you pick one of its
  calls, newest first). Or click **where from?** next to a value in **Show Values** (an
  argument, the result, a local an exception left, a value over time), or **Where From?** on a
  call or a value in the Calls view. The **Origin** view (in the Luciole sidebar) answers with the
  chain that made the value: the assignment, the caller that passed it, the call that returned
  it, the last call that set a global or an attribute, then what fed each of those, with values,
  functions, call numbers (`#12`), lines and code. Click a step to open its line and make its
  function pulse on the brain. The brain lights the path: its functions stand out, every other
  one is dimmed, and each move of the value from one function to the next traces its line in the
  accent, from the sources toward the question; the camera frames the whole path. When a local
  is assigned on several lines, the answer lists them and offers **Record … line by line and
  run again**: the same command runs again with that function's variables recorded (level 2);
  ask again when it ends. Where the walk stopped (its depth limit), **go on from here** asks
  again from that step. **Clear Origin** (the view's title bar) clears it; a Run Live or a
  replay starting clears it too, since the brain then shows the run. See
  [docs/origins.md](../docs/origins.md) for what each recording level can answer.
- **Focus and filters**: **Focus Around** (on a function in the Map or a call in Calls, or from
  the command palette) shows only the functions a few calls around one, callers and callees (the
  `luciole.focusDepth` setting, 1 by default); the others are dimmed, nearly to the background. **Luciole: Filters…** (the
  filter button of the Map) hides tests (test files, `test_` functions, `Test` classes) and private
  helpers (`_name`). Filtered functions stay where they are, nearly invisible, so the map keeps its
  shape; **Clear filters** in the panel shows everything again.
- **The Luciole sidebar** (its icon in the activity bar) has four views. **Actions**: the graph
  and Run Live commands, and the auto-start switch with its state. **Running**: the programs lit
  up now (Run Live, programs started with auto-start on, tests, their child processes), with their
  command line, pid and state, and the last few that exited; a program in one of VS Code's debug
  sessions has a button to the Call Stack and is stopped from the debug toolbar, any other has a
  Stop button (press it again to kill it). **Calls**: the recorded runs (see History above).
  **Origin** (while there is an answer): where a value came from (see above).
  **Map**: an outline of the graph in the panel,
  packages → modules → classes → functions, with how many functions each holds and which are running. Click an item and the
  view flies to it in the brain; a function also has **Go to Source**. The search button
  (**Luciole: Find in Map**) lists every function to fly to; typing in the Map filters what is
  expanded.

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
debugger, the Python Debugger extension (`ms-python.debugpy`); without it, Run Live runs in a
terminal (the brain lights up, no breakpoints or stepping) and the panel offers to install it.
Nothing needs installing in your project. Recordings stay on your machine, in `~/.luciole/runs`
(the last 50 runs of each project); nothing is sent anywhere.
