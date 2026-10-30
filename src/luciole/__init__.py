"""Luciole: a Python project as an explorable graph, lit up as the code runs."""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.2.0"


def start(
    root: Path | str | None = None,
    port: int | str | None = None,
    events: Path | str | None = None,
    trace: Path | str | None = None,
    level: int | None = None,
    scope: str | list[str] | None = None,
) -> bool:
    """Light up the Luciole view with this process's calls, until stop() or the process exits.

    For code you start yourself (a server, a notebook); `luciole run` and auto-start do the same
    for whole programs. `root` is the project whose code is reported (default: LUCIOLE_ROOT, else
    the current directory), `port` the view's (default: LUCIOLE_LIVE_PORT), `events` a JSON
    Lines file to write the events to as well, and `trace` a SQLite file (or a directory to make
    one in) to write a trace of the run to, at `level` 0, 1 or 2 (default: LUCIOLE_TRACE and
    LUCIOLE_TRACE_LEVEL; see docs/trace-format.md). Level 2 records every assignment in the
    functions of `scope`: node id patterns, as a list or comma-separated (`cart:checkout`,
    `Cart.*`; default: LUCIOLE_TRACE_SCOPE); without one, it records at level 1, with a warning.

    Returns False, and records nothing, when nothing would see the events: no view listens on
    `port` and there is neither an `events` file nor a `trace`, or Python is older than 3.12.
    """
    from luciole import session

    return session.start(root, port, events, trace=trace, level=level, scope=scope)


def stop() -> None:
    """Stop recording this process (the view shows the run as finished)."""
    session = sys.modules.get("luciole.session")
    if session is not None:
        session.stop()
