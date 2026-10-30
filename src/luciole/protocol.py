"""The live protocol between recorders, the engine and viewers: JSON lines over a local socket.

Every connection to the engine starts with a hello, which the engine answers before anything else:

    -> {"hello": {"role": "recorder", "protocol": 1, "version": "0.1.0", "pid": 4242,
                  "argv": ["python", "-m", "pytest"], "root": "/work/app"}}
    -> {"hello": {"role": "viewer", "protocol": 1, "version": "0.1.0", "name": "vscode",
                  "roots": ["/work/app"]}}
    <- {"welcome": {"protocol": 1, "version": "0.1.0"}}
    <- {"refused": "..."}                    (then the engine closes the connection)

A starting engine checks that the one named in the file runs with the role "probe": it is
welcomed, and the connection closed.

PROTOCOL changes when a message changes meaning; versions of Luciole that speak the same protocol
work together. The running engine is found through its file in ~/.luciole (LUCIOLE_HOME), one per
protocol, so engines of different protocols can run side by side. See docs/graph-format.md.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from luciole import __version__

PROTOCOL = 1


def home() -> Path:
    return Path(os.environ.get("LUCIOLE_HOME") or Path.home() / ".luciole")


def engine_file() -> Path:
    return home() / f"engine-v{PROTOCOL}.json"


def engine_port() -> int | None:
    """Port of the running engine, from its file; None when there is none."""
    try:
        return int(json.loads(engine_file().read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def hello(role: str, **fields) -> dict:
    return {"hello": {"role": role, "protocol": PROTOCOL, "version": __version__, **fields}}


def send(connection: socket.socket, message: dict) -> None:
    connection.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode())
