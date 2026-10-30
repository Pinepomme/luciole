# Changelog

## Unreleased

- Live mode: a process that ends within milliseconds of starting (a multiprocessing pool's
  workers, terminated right after their last task) no longer loses its calls. The recorder now
  reads the engine's welcome before the program goes on (waiting half a second at most); left unread, it made the connection
  reset when the process ended, and the engine dropped the calls it had not read yet.
- Each file's functions get a muted tint of their own (Apple's system hues, half-way to grey), so you see which module a function is in; activity stays in the accent.
- A calmer, minimal look: neutral greys at rest and one accent color for activity, the theme's
  own (its link color), with red only for breakpoints and the debugger. Smaller matte neurons,
  hairline links, names in the system font with no outlines; no colors per region, no glows.
  A running function fills with the accent and gets a thin ring, a coroutine waiting on an
  `await` gets the ring only. Works in light and dark themes. The legend is gone: hover a line to
  see what it is.
- Live mode: a call traces its line from the caller to the callee in the accent color, like a
  route drawing itself on a map (instead of bubbles), then eases back to grey. A busy line stays
  drawn until its calls stop.
- The brain is a map: packages, modules and classes are regions inside regions, placed by how
  much they call each other, with their names at their centres. The same project always gives
  the same map, and editing a file leaves the rest in place. Calls between regions travel
  together in tracts instead of thousands of crossing lines.
- Run Live (⚡, Run Live…, run configurations, tests for a file, Run again) runs the program under
  VS Code's Python debugger, lit up by the recorder: breakpoints, stopping on exceptions, stepping,
  the Variables, Watch and Call Stack views, hover and the Debug Console all work on it. Without
  the Python Debugger extension (`ms-python.debugpy`), it runs in a terminal as before, lighting
  only. Luciole's own Pause/Resume and Step commands, their keys (F10, F11, Shift+F11, F5) and
  buttons, and the Luciole Variables view are removed: the debugger's do the same. The recorder
  keeps its pause, step and variables protocol for other clients of the engine.
- Auto-start: with the **Luciole: on** switch (status bar, setting `luciole.autoStart`), every
  Python program started in a new VS Code terminal lights up the view, child processes included
  (forked ones too). Nothing is started when the view is not listening. `luciole env` does the
  same for any shell, and `luciole.start()` / `luciole.stop()` from inside a program.
- Run Live on a library module (no `if __name__ == "__main__":`) runs the test files that import
  it with pytest, and on a test file runs pytest on it. `luciole tests FILE` lists those tests.
- The Luciole engine (`luciole engine`): one local process that recording programs connect to and
  views subscribe to, started by the extension when none runs and found through
  `~/.luciole/engine-v1.json`. Every connection starts with a handshake that checks the protocol
  version. Each VS Code window sees the programs of its own folders. `luciole run` and
  `luciole env` no longer need a port.
- Debugging: the brain follows VS Code's Python debugger (the function it stopped in glows red),
  right-click a neuron to stop on each call of its function, and with auto-start on, debugged
  programs light up too. Function nodes gain `body_line`, the first line each call runs.
- Better call resolution: imports (which run the imported module), star imports, properties,
  `super()`, overrides in subclasses, return types and annotations, special methods (`x[i]`,
  `with`, `len`, f-strings…), functions held in variables, and, as a marked guess (`by_name`), the
  project's only method of a name. On the standard library's `email` and `asyncio` packages, the
  share of runtime calls missing from the graph went from 52% to 27%.
- Callbacks: a function passed, not called, as an argument (`Thread(target=crunch)`,
  `pool.submit(work)`, `sorted(key=rank)`, `button.bind(self.on_click)`) is linked to the function
  passing it (`calls` edges with `"callback": true`), so threads and handlers no longer float apart.
- The view shows several processes at once; Stop applies to all of them (terminal runs).
- The graph and `luciole tests` skip the folders the project's `.gitignore` lists (data, runs,
  outputs), and `luciole tests` also finds tests that reach a module through other modules.
- A process ended by SIGTERM (a `multiprocessing` pool's workers) still reports its last calls.
- Labels sit above their neuron, and overlapping labels give way to the more important one.
- The recorder uses a `sys.monitoring` tool id of its own, so `cProfile`, coverage and debuggers
  keep working with it.

## 0.1.0

First release.

- `luciole graph`: functions, classes, variables, calls and variable flow of a Python project,
  from its source.
- `luciole run`: runs a script or module under a recorder (`sys.monitoring`, Python 3.12+) that
  reports calls, call paths, threads and asyncio tasks, and can pause, step and show variables.
- VS Code extension: a 3D graph of the project, lit up as the program runs. Run Live on the
  current file, Run Live… for arguments, modules (pytest) and run configurations, and Stop,
  Pause/Resume, Step over/into/out with a Variables view.
