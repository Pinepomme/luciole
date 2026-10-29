"""The engine: the one local process that recorders connect to and viewers subscribe to.

    luciole engine [--port PORT] [--idle SECONDS]

Each Python process that records (`luciole run`, luciole.start(), auto-start) is a recorder; the
VS Code extension, and later agents and other editors, are viewers. After the hello (see
protocol.py), the engine numbers each recorder and passes on what it sends to the viewers that
show its project, with the recorder's number:

    <- {"process": {"id": 3, "pid": 4242, "argv": [...], "root": "/work/app"}}
    <- {"from": 3, "t": 1.25, "stacks": [...], "calls": {...}}     (any recorder message)
    <- {"exit": 3}

and passes viewers' commands on to the recorders: {"to": 3, "cmd": "step", ...} to one, a
command without "to" to every recorder the viewer shows. A viewer shows a recorder when one's
root contains the other's, symbolic links resolved (a viewer without roots shows them all), so
two VS Code windows on two projects each see their own programs. When a viewer leaves, the programs
no remaining viewer shows resume.

The engine writes its port to ~/.luciole/engine-v<protocol>.json for the others to find it, and
exits after `idle` seconds with nobody connected. Standard library only, one thread per
connection: a handful of processes and viewers, each sending a line every 50 ms at most.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path

from luciole import __version__
from luciole.protocol import PROTOCOL, engine_file, engine_port, send

log = logging.getLogger(__name__)


class Peer:
    def __init__(self, connection: socket.socket):
        self.connection = connection
        self.role = ""
        self.id = 0
        self.info: dict = {}
        self.roots: list[Path] = []  # a viewer's, a recorder's own: resolved (see real_path)
        self.lock = threading.Lock()  # lines from several threads must not interleave

    def send_line(self, line: str) -> bool:
        try:
            with self.lock:
                self.connection.sendall(line.encode())
            return True
        except OSError:
            return False

    def send(self, message: dict) -> bool:
        return self.send_line(json.dumps(message, separators=(",", ":")) + "\n")


class Engine:
    def __init__(self, port: int = 0, idle: float = 600):
        self.server = socket.create_server(("127.0.0.1", port))
        self.port = self.server.getsockname()[1]
        self.idle = idle
        self.lock = threading.Lock()
        self.recorders: dict[int, Peer] = {}
        self.viewers: list[Peer] = []
        self.peers = 0
        self.ids = itertools.count(1)
        self.quiet_since = time.monotonic()
        self.stopping = threading.Event()

    # Running

    def serve(self) -> None:
        """Accept connections until stop(), or `idle` seconds with nobody connected."""
        self.register()
        self.server.settimeout(0.5)
        try:
            while not self.stopping.is_set():
                try:
                    connection, _ = self.server.accept()
                except TimeoutError:
                    with self.lock:
                        if self.idle and not self.peers and time.monotonic() - self.quiet_since > self.idle:
                            return
                    continue
                connection.settimeout(None)
                with self.lock:
                    self.peers += 1
                threading.Thread(target=self.handle, args=(Peer(connection),), daemon=True).start()
        finally:
            self.server.close()
            self.unregister()

    def stop(self) -> None:
        self.stopping.set()

    def register(self) -> None:
        file = engine_file()
        file.parent.mkdir(parents=True, exist_ok=True)
        temporary = file.with_name(f"{file.name}.{os.getpid()}")
        temporary.write_text(json.dumps({"port": self.port, "pid": os.getpid(), "protocol": PROTOCOL, "version": __version__}))
        os.replace(temporary, file)

    def unregister(self) -> None:
        if engine_port() == self.port:
            engine_file().unlink(missing_ok=True)

    # Connections

    def handle(self, peer: Peer) -> None:
        try:
            with peer.connection, peer.connection.makefile(encoding="utf-8") as lines:
                first = next(lines, "")
                if self.greet(peer, first):
                    (self.from_recorder if peer.role == "recorder" else self.from_viewer)(peer, lines)
        except (OSError, ValueError):
            pass
        finally:
            self.leave(peer)

    def greet(self, peer: Peer, line: str) -> bool:
        try:
            hello = json.loads(line)["hello"]
            role, protocol = hello["role"], hello["protocol"]
        except (ValueError, KeyError, TypeError):
            peer.send({"refused": "expected a hello"})
            return False
        if protocol != PROTOCOL or role not in ("recorder", "viewer"):
            peer.send({"refused": f"this engine speaks protocol {PROTOCOL} (Luciole {__version__}), not {protocol}"})
            return False
        peer.role = role
        peer.send({"welcome": {"protocol": PROTOCOL, "version": __version__}})
        with self.lock:
            if role == "recorder":
                peer.id = next(self.ids)
                peer.info = {"id": peer.id, "pid": hello.get("pid"), "argv": hello.get("argv", []), "root": hello.get("root")}
                peer.roots = [real_path(hello["root"])] if hello.get("root") else []
                self.recorders[peer.id] = peer
                viewers = [viewer for viewer in self.viewers if self.shows(viewer, peer)]
            else:
                peer.roots = [real_path(root) for root in hello.get("roots", [])]
                self.viewers.append(peer)
                recorders = [recorder for recorder in self.recorders.values() if self.shows(peer, recorder)]
        if role == "recorder":
            for viewer in viewers:
                viewer.send({"process": peer.info})
        else:
            for recorder in recorders:
                peer.send({"process": recorder.info})
        return True

    def from_recorder(self, peer: Peer, lines) -> None:
        prefix = '{"from":%d,' % peer.id
        for line in lines:
            if not line.endswith("\n"):
                break  # cut off by the recorder's end: its exit must reach viewers on a line of its own
            if not line.startswith("{") or line.startswith("{}"):
                continue
            tagged = prefix + line[1:]  # the recorder's number goes in without parsing the line again
            with self.lock:
                viewers = [viewer for viewer in self.viewers if self.shows(viewer, peer)]
            for viewer in viewers:
                viewer.send_line(tagged)

    def from_viewer(self, peer: Peer, lines) -> None:
        for line in lines:
            try:
                command = json.loads(line)
                target = command.pop("to", None)
            except (ValueError, AttributeError):
                continue
            with self.lock:
                if target is None:
                    recorders = [recorder for recorder in self.recorders.values() if self.shows(peer, recorder)]
                else:
                    recorders = [self.recorders[target]] if target in self.recorders else []
            for recorder in recorders:
                recorder.send(command)

    def leave(self, peer: Peer) -> None:
        with self.lock:
            self.peers -= 1
            if not self.peers:
                self.quiet_since = time.monotonic()
            if peer.role == "recorder":
                self.recorders.pop(peer.id, None)
                viewers = [viewer for viewer in self.viewers if self.shows(viewer, peer)]
                orphans = []
            elif peer.role == "viewer":
                self.viewers.remove(peer)
                viewers = []
                # Paused programs that no viewer shows any more (one may have paused it with "to").
                orphans = [recorder for recorder in self.recorders.values() if not any(self.shows(viewer, recorder) for viewer in self.viewers)]
            else:
                return
        for viewer in viewers:
            viewer.send({"exit": peer.id})
        for recorder in orphans:
            recorder.send({"cmd": "resume"})  # nobody left to resume a paused program

    @staticmethod
    def shows(viewer: Peer, recorder: Peer) -> bool:
        if not viewer.roots or not recorder.roots:
            return True
        [root] = recorder.roots
        return any(root.is_relative_to(shown) or shown.is_relative_to(root) for shown in viewer.roots)


def real_path(path: str) -> Path:
    """`path` with symbolic links resolved, so that a workspace opened through a link shows the
    programs running in its target (recorders resolve their root). Paths need not exist."""
    return Path(os.path.realpath(path))


def serve(port: int = 0, idle: float = 600) -> int:
    """`luciole engine`: run an engine unless one already answers. Returns the exit code."""
    running = engine_port()
    if running and running != port:
        try:
            socket.create_connection(("127.0.0.1", running), timeout=1).close()
            log.warning("an engine already runs on port %s", running)
            return 0
        except OSError:
            pass  # a stale file: the engine that wrote it is gone
    engine = Engine(port, idle)
    log.warning("engine on port %s (protocol %s)", engine.port, PROTOCOL)
    engine.serve()
    return 0
