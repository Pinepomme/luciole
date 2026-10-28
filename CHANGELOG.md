# Changelog

## Unreleased

- Auto-start: with the **Luciole: on** switch (status bar, setting `luciole.autoStart`), every
  Python program started in a new VS Code terminal lights up the view, child processes included
  (forked ones too). Nothing is started when the view is not listening. `luciole env` does the
  same for any shell, and `luciole.start()` / `luciole.stop()` from inside a program.
- Run Live on a library module (no `if __name__ == "__main__":`) runs the test files that import
  it with pytest, and on a test file runs pytest on it. `luciole tests FILE` lists those tests.
- The view shows several processes at once; Pause and Stop apply to all of them.
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
