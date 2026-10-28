# Changelog

## 0.1.0

First release.

- `luciole graph`: functions, classes, variables, calls and variable flow of a Python project,
  from its source.
- `luciole run`: runs a script or module under a recorder (`sys.monitoring`, Python 3.12+) that
  reports calls, call paths, threads and asyncio tasks, and can pause, step and show variables.
- VS Code extension: a 3D graph of the project, lit up as the program runs. Run Live on the
  current file, Run Live… for arguments, modules (pytest) and run configurations, and Stop,
  Pause/Resume, Step over/into/out with a Variables view.
