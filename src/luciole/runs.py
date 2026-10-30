"""Run folders: where recorded runs are kept, for the MCP server's `run` and VS Code's Run Live alike.

    ~/.luciole/runs/<project>-<hash>/<run id>/
        command.json     what ran: command, folder, level, scope, who started it, how it ended
        output.txt       the program's output (MCP runs; Run Live's is in its terminal)
        *.sqlite         one trace per process (docs/trace-format.md)

A run id is the time it started (`20261030-142501`, then `-1`, `-2`... in the same second), so
sorting ids sorts runs. The last KEEP_RUNS runs of a project are kept.

    luciole runs [--root R] [--json]                                 the runs, newest first, with their processes
    luciole runs new --root R --command '["python", "app.py"]' [--cwd C] [--level 1] [--by vscode]   a new run folder (prints it)
    luciole runs finish FOLDER [--exit-status N]                     the run ended
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

from luciole import protocol
from luciole.trace import Trace, TraceError

KEEP_RUNS = 50  # run folders kept per project; older ones are deleted


def runs_folder(root: Path) -> Path:
    """Where a project's runs are kept: ~/.luciole/runs/<name>-<hash of its path>."""
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:8]
    return protocol.home() / "runs" / f"{root.name or 'root'}-{digest}"


def new_run(root: Path, command: list[str], cwd: Path, level: int, scope: list[str] | None = None, by: str = "agent") -> tuple[str, Path]:
    """A new, empty run folder for `root` with its command.json; the oldest runs past KEEP_RUNS go."""
    runs_dir = runs_folder(root)
    runs_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # After the last run of this second, never in a gap a pruned one left: ids sort as runs started.
    taken = [path.name[len(stamp) :] for path in runs_dir.glob(f"{stamp}*")]
    first = max((int(rest[1:]) + 1 if rest[1:].isdigit() else 1 for rest in taken), default=0)
    for n in range(first, first + 1000):
        run_id = stamp if n == 0 else f"{stamp}-{n}"
        folder = runs_dir / run_id
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        meta = {"command": command, "cwd": str(cwd), "level": level, "scope": scope or None, "by": by, "started_at": time.time()}
        (folder / "command.json").write_text(json.dumps(meta))
        old = sorted(path for path in runs_dir.iterdir() if path.is_dir() and path != folder)
        for path in old[: max(0, len(old) - KEEP_RUNS + 1)]:
            shutil.rmtree(path, ignore_errors=True)
        return run_id, folder
    raise OSError("could not make a run folder")


def finish_run(folder: Path, exit_status: int | None) -> None:
    """The run ended (`exit_status` None: unknown, or stopped by a timeout)."""
    path = folder / "command.json"
    meta = json.loads(path.read_text())
    path.write_text(json.dumps({**meta, "exit_status": exit_status, "ended_at": time.time()}))


def processes_of(folder: Path) -> list[Path]:
    """A run's trace files, the first process first (then by start time)."""
    found = []
    for path in folder.glob("*.sqlite"):
        try:
            with Trace(path) as trace:
                run = trace.run()
        except (TraceError, TypeError, sqlite3.Error):
            continue  # not a trace, or one still being created
        found.append((run["started_at"], path))
    return [path for _, path in sorted(found)]


def list_runs(root: Path) -> dict:
    """The runs of `root`, newest first: `{"root", "folder", "runs": [...]}`. Each run is its
    command.json plus `id`, `folder`, `state` (`running` until whoever started it says it ended,
    `finished`, `empty`: ended with nothing recorded) and `processes`, each `{"file", "pid", "argv", "started_at", "duration_ns",
    "ended", "exit_status", "ended_by", "frames", "exceptions", "uncaught", "level", "scope",
    "truncated", "first_ns"}` (`first_ns`: when its first call started, where a replay starts)."""
    root = root.resolve()
    runs_dir = runs_folder(root)
    found = []
    for path in sorted(runs_dir.glob("*/command.json"), reverse=True) if runs_dir.is_dir() else []:
        try:
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # being written
        processes = [process_info(file) for file in processes_of(path.parent)]
        # A process that ended without running project code (a launcher, pip, a linter) is left out.
        processes = [process for process in processes if process and (process["frames"] or not process["ended"])]
        meta.setdefault("by", "agent")  # runs made before `by` existed were the MCP server's
        ended = "ended_at" in meta or "exit_status" in meta
        if not ended and processes and processes[0]["ended"] and processes[0]["exit_status"] is not None:
            ended = True  # luciole run wrote it (a terminal Run Live)
            meta["exit_status"] = processes[0]["exit_status"]
        state = "running" if not ended else "finished" if processes else "empty"
        found.append({**meta, "id": path.parent.name, "folder": str(path.parent), "state": state, "processes": processes})
    return {"root": str(root), "folder": str(runs_dir), "runs": found}


def process_info(path: Path) -> dict | None:
    try:
        with Trace(path) as trace:
            run = trace.run()
            frames, exceptions, uncaught, first = trace.db.execute(
                "SELECT (SELECT COUNT(*) FROM frames), (SELECT COUNT(*) FROM exceptions),"
                " (SELECT COUNT(*) FROM exceptions WHERE handled_in IS NULL AND frame IS NOT NULL), (SELECT MIN(start_ns) FROM frames)"
            ).fetchone()
    except (TraceError, sqlite3.Error):
        return None
    return {
        "file": str(path),
        "pid": run["pid"],
        "argv": run["argv"],
        "started_at": run["started_at"],
        "duration_ns": run["duration_ns"],
        "ended": run["ended_at"] is not None,
        "exit_status": run["exit_status"],
        "ended_by": run["ended_by"],
        "frames": frames,
        "exceptions": exceptions,
        "uncaught": uncaught,
        "level": run["level"],
        "scope": run["scope"],
        "truncated": run["truncated"],
        "first_ns": first or 0,
    }


def render_runs(listing: dict) -> str:
    if not listing["runs"]:
        return f"no runs of {listing['root']} yet"
    lines = []
    for run in listing["runs"]:
        status = run.get("exit_status")
        outcome = {"running": "running", "empty": "nothing recorded"}.get(run["state"]) or ("finished" if status is None else f"exit status {status}")
        lines.append(f"{run['id']}  {' '.join(run['command'])}  ({outcome}, by {run['by']})")
        for process in run["processes"]:
            lines.append(f"    pid {process['pid']}: {process['frames']} calls, {process['exceptions']} exceptions  {process['file']}")
    return "\n".join(lines)
