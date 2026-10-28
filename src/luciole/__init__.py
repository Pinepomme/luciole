"""Luciole: a Python project as an explorable graph, lit up as the code runs."""

from __future__ import annotations

import sys
from pathlib import Path

__version__ = "0.1.0"


def start(root: Path | str | None = None, port: int | str | None = None, events: Path | str | None = None) -> bool:
    """Light up the Luciole view with this process's calls, until stop() or the process exits.

    For code you start yourself (a server, a notebook); `luciole run` and auto-start do the same
    for whole programs. `root` is the project whose code is reported (default: LUCIOLE_ROOT, else
    the current directory), `port` the view's (default: LUCIOLE_LIVE_PORT), and `events` a JSON
    Lines file to write the events to as well.

    Returns False, and records nothing, when nothing would see the events: no view listens on
    `port` and there is no `events` file, or Python is older than 3.12.
    """
    from luciole import recorder

    return recorder.start(root, port, events)


def stop() -> None:
    """Stop recording this process (the view shows the run as finished)."""
    recorder = sys.modules.get("luciole.recorder")
    if recorder is not None:
        recorder.stop()
