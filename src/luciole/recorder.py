"""Live recorder: report which project functions run while a program runs (Python 3.12+).

`sys.monitoring` reports every call of project code (other code is disabled after its
first event, so it costs nothing). Every FRAME seconds a background thread sends one
JSON line with what happened since the last one, and the current state:

    {"t": 1.25,
     "stacks": [{"thread": "MainThread", "stack": ["shop", "shop:main", "shop:Cart.total"]}],
     "calls": {"shop:Cart.add": 2}, "links": {"shop:main>shop:Cart.add": 2},
     "spawns": {"app:serve>app:worker": 1}, "waiting": {"app:fetch": 3}}

- `stacks`: each thread's project frames, outermost first, main thread first.
- `calls`, `links`: calls per function, and per caller>callee (the caller being the nearest
  project frame, through library code), since the previous line.
- `spawns`: work started elsewhere since the previous line: a thread started by a project
  function, or an asyncio task whose coroutine a project function created, when that is
  not simply the caller.
- `waiting`: project coroutines suspended on an `await` right now, per function.
The last line is {"done": true}. Node ids follow docs/graph-format.md, so they match the
static graph. The connection to the engine starts with the recorder's hello (see protocol.py).

A session starts with `luciole run`, with luciole.start() in the program, or by itself in any
Python process started with the auto-start folder on PYTHONPATH (see _autostart/sitecustomize.py).
Forked children connect again as processes of their own.

The view can pause the program to look at its variables, with JSON lines sent back on the
socket: {"cmd": "pause"}, {"cmd": "resume"}, {"cmd": "expand", "ref": 7},
{"cmd": "step", "thread": 1401, "mode": "over"}. Pausing turns on LINE events: each thread stops
at its next line of project code, sends its own stack with the locals of each project frame
({"paused": {...}}), and then waits, answering "expand" requests for the values it owns
({"children": {...}}), until "resume" or "step". A step resumes every thread; the stepping one
stops at its next project line ("into"), the next line of the same frame or of its caller once
the frame has left ("over"), or the caller's next line ("out"). Then all threads stop again.

Every piece of state is kept by the monitoring callbacks, in the thread the event happens
in: each thread its own project stack (PY_START / PY_RESUME push, PY_RETURN / PY_YIELD /
PY_UNWIND pop). The background thread only reads those plain lists and dicts, never other
threads' frames, since `sys._current_frames()` breaks torch.export tracing (FakeTensor
without fake_mode, missing lifted constants).
"""

from __future__ import annotations

import _thread
import atexit
import inspect
import json
import itertools
import logging
import os
import queue
import runpy
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from luciole import protocol, values
from luciole.analyzer import SKIP_DIRS, module_name

if TYPE_CHECKING:
    from luciole.tracer import Tracer  # imported when tracing only: sqlite3 costs a few ms

log = logging.getLogger(__name__)

FRAME = 0.05  # seconds between two frames
OWN_PACKAGE = Path(__file__).resolve().parent
ASYNC_FLAGS = inspect.CO_COROUTINE | inspect.CO_ITERABLE_COROUTINE | inspect.CO_ASYNC_GENERATOR
CALLER_SEARCH = 40  # frames to look through first for the nearest project caller (asyncio adds ~10)
ORIGIN_DEPTH = 8  # frames recorded where each coroutine object is created
MAX_PAUSED_FRAMES = 60


STEP_MODES = ("over", "into", "out")


def own_thread(target, name: str) -> threading.Thread:
    """A thread of the recorder's own. Marked the way debugpy marks its threads, so that VS Code's
    debugger neither lists nor pauses it: the view stays live while the debugger is stopped."""
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.is_pydev_daemon_thread = True
    thread.pydev_do_not_trace = True
    return thread


class Step:
    """A step in progress in `thread`, from `frame` (where it was paused)."""

    __slots__ = ("thread", "frame", "mode", "left")

    def __init__(self, thread: int, frame, mode: str):
        self.thread = thread
        self.frame = frame
        self.mode = mode
        self.left = False  # `frame` returned, raised, or (a generator) yielded

    def stops_at(self, frame) -> bool:
        """Whether the thread's next project line, in `frame`, ends the step."""
        if self.mode == "into" or self.left:
            return True
        return self.mode == "over" and frame is self.frame


class Recorder:
    def __init__(self, root: Path, send: Callable[[dict], None], reply: Callable[[dict], None] = lambda message: None):
        self.root = root.resolve()
        self.send = send
        self.reply = reply  # to the view only, not to the --events file
        self.ids: dict[object, str | None] = {}  # code object -> node id, None for code outside the project
        self.modules: dict[str, str | None] = {}  # file name -> module name, None outside the project
        self.lines: dict[str, list[tuple[int, int, str]]] = {}  # file -> (first line, last line, node id)
        self.calls: dict[str, int] = {}
        self.links: dict[str, int] = {}
        self.spawns: dict[str, int] = {}
        self.waiting: dict[str, int] = {}
        self.started = time.perf_counter()
        self.thread_stacks: dict[int, list[str]] = {}  # thread ident -> project stack, replaced (never mutated)
        self.last_state: tuple = ([], {})  # nothing runs yet: no frame until something does
        # Pausing: set from the control thread; each paused thread waits on its own queue.
        self.line_events: Callable[[bool], None] = lambda on: None
        self.pausing = False
        self.pause_lock = threading.Lock()
        self.paused: dict[int, queue.SimpleQueue] = {}  # thread ident -> "expand" requests, None to resume
        self.refs: dict[int, tuple[int, object]] = {}  # ref -> (owning thread ident, value)
        self.next_ref = itertools.count(1)
        self.step: Step | None = None
        self.tracer: Tracer | None = None  # writes the run to a trace file, when asked to

    # Identifying code

    def node_id(self, code) -> str | None:
        try:
            return self.ids[code]
        except KeyError:
            pass
        node_id = None
        module = self.module_of(code.co_filename)
        if module:
            qualname = code.co_qualname.replace(".<locals>.", ".")
            if qualname == "<module>":
                node_id = module
            elif "<" not in qualname:  # lambdas and generator expressions have no node
                node_id = f"{module}:{qualname}"
        self.ids[code] = node_id
        if node_id:
            last = max((line for _, _, line in code.co_lines() if line), default=code.co_firstlineno)
            self.lines.setdefault(code.co_filename, []).append((code.co_firstlineno, last, node_id))
        return node_id

    def module_of(self, filename: str) -> str | None:
        """Module name of a project file, None for other files. Resolving paths touches the disk: once per file."""
        try:
            return self.modules[filename]
        except KeyError:
            pass
        module = None
        if not filename.startswith("<"):
            path = Path(filename).resolve()
            if self.in_project(path):
                module = module_name(path)
        self.modules[filename] = module
        return module

    def in_project(self, path: Path) -> bool:
        if path.suffix != ".py" or not path.is_relative_to(self.root) or path.is_relative_to(OWN_PACKAGE):
            return False
        parts = path.relative_to(self.root).parent.parts
        return not any(part.startswith(".") or part in SKIP_DIRS for part in parts)

    def nearest_project(self, frame, limit: int | None = CALLER_SEARCH) -> str | None:
        """Node id of the nearest project frame, starting at `frame` (in the current thread)."""
        found = self.project_frame(frame, limit)
        return self.ids[found.f_code] if found else None

    def project_frame(self, frame, limit: int | None = CALLER_SEARCH):
        """The nearest project frame, starting at `frame` (in the current thread).

        Looks through `limit` frames, or the whole stack when `limit` is None.
        """
        depth = 0
        while frame is not None and (limit is None or depth < limit):
            if self.node_id(frame.f_code):
                return frame
            frame = frame.f_back
            depth += 1
        return None

    def locate(self, filename: str, line: int) -> str | None:
        """Innermost project function seen so far that contains this line."""
        spans = [(last - first, node_id) for first, last, node_id in self.lines.get(filename, ()) if first <= line <= last]
        return min(spans)[1] if spans else None

    def task_origin(self, code) -> str | None:
        """Project function that created the coroutine, when it is the running asyncio task's own."""
        asyncio = sys.modules.get("asyncio")
        try:
            task = asyncio and asyncio.current_task()
        except RuntimeError:  # no running event loop in this thread
            return None
        coro = task.get_coro() if task else None
        if getattr(coro, "cr_code", None) is not code:
            return None  # an inner coroutine, awaited by its caller: no spawn
        origin = getattr(coro, "cr_origin", None)
        for filename, line, _ in origin or ():
            node_id = self.locate(filename, line)
            if node_id:
                return node_id
        return None

    # Monitoring callbacks

    def on_start(self, code, offset: int):
        """PY_START: a function, module, generator or coroutine starts running."""
        node_id = self.node_id(code)
        if node_id is None:
            return sys.monitoring.DISABLE
        self.enter(code, node_id, sys._getframe(1), new_call=True)
        return None

    def on_resume(self, code, offset: int):
        """PY_RESUME: a generator or coroutine resumes after a yield or await."""
        node_id = self.node_id(code)
        if node_id is None:
            return sys.monitoring.DISABLE
        self.resume(code, node_id, sys._getframe(1))
        return None

    def on_throw(self, code, offset: int, exception):
        """PY_THROW: like a resume, with an exception (cancellation, close). Cannot be disabled."""
        node_id = self.node_id(code)
        if node_id:
            self.resume(code, node_id, sys._getframe(1))

    def on_yield(self, code, offset: int, value):
        """PY_YIELD: a generator yields, or a coroutine suspends on an await."""
        node_id = self.node_id(code)
        if node_id is None:
            return sys.monitoring.DISABLE
        self.leave(node_id)
        if self.tracer is not None:
            self.tracer.suspend(code)
        if code.co_flags & ASYNC_FLAGS:
            self.waiting[node_id] = self.waiting.get(node_id, 0) + 1
        elif self.step is not None:
            self.left(sys._getframe(1))  # a generator yields to its caller; a coroutine resumes later
        return None

    def on_return(self, code, offset: int, value):
        """PY_RETURN: a project function returns."""
        node_id = self.node_id(code)
        if node_id is None:
            return sys.monitoring.DISABLE
        self.leave(node_id)
        if self.tracer is not None:
            self.tracer.returned(code, value)
        if self.step is not None:
            self.left(sys._getframe(1))
        return None

    def on_unwind(self, code, offset: int, exception):
        """PY_UNWIND: a function exits with an exception. Cannot be disabled."""
        node_id = self.node_id(code)
        if node_id:
            self.leave(node_id)
            if self.tracer is not None:
                self.tracer.unwound(code, exception)
            if self.step is not None:
                self.left(sys._getframe(1))

    def on_raise(self, code, offset: int, exception):
        """RAISE, only while tracing: an exception is raised in a frame, or reaches it from a call.
        Cannot be disabled."""
        if self.tracer is not None and self.node_id(code):
            self.tracer.raised(code, sys._getframe(1), exception)

    def left(self, frame) -> None:
        step = self.step
        if step is not None and step.frame is frame:
            step.left = True

    def on_line(self, code, line: int):
        """LINE, only on while pausing or stepping: project code stops here."""
        if self.node_id(code) is None:
            return sys.monitoring.DISABLE
        ident = threading.get_ident()
        step = self.step
        if step is not None and step.thread == ident and step.stops_at(sys._getframe(1)):
            with self.pause_lock:  # the step is over: everyone stops again
                self.step = None
                self.pausing = True
        if self.pausing and ident not in self.paused:
            self.hold(sys._getframe(1))
        return None

    # Pausing

    def command(self, command: dict) -> None:
        """A command from the view, in the control thread."""
        kind = command.get("cmd")
        if kind == "pause":
            with self.pause_lock:
                self.pausing = True
                self.line_events(True)
        elif kind == "resume":
            self.resume_all()
        elif kind == "step" and command.get("mode", "over") in STEP_MODES:
            self.step_from(command.get("thread"), command.get("mode", "over"))
        elif kind == "expand":
            ref = command.get("ref")
            owner, _ = self.refs.get(ref, (None, None))
            requests = self.paused.get(owner)
            if requests:
                requests.put(ref)  # the owning thread describes its own values
            else:
                self.reply({"children": {"ref": ref, "vars": []}})

    def step_from(self, ident: int, mode: str) -> None:
        """Resume every thread; `ident` sets up its step when it wakes (LINE events stay on)."""
        with self.pause_lock:
            if ident not in self.paused:
                return
            self.pausing = False
            for owner, requests in self.paused.items():
                requests.put(mode if owner == ident else None)
        self.refs.clear()

    def resume_all(self) -> None:
        with self.pause_lock:
            self.step = None
            self.pausing = False
            self.line_events(False)
            for requests in self.paused.values():
                requests.put(None)
        self.refs.clear()

    def hold(self, frame) -> None:
        """Pause this thread at `frame`: send its stack, then answer requests until resumed."""
        ident = threading.get_ident()
        requests = queue.SimpleQueue()
        with self.pause_lock:
            if not self.pausing:
                return  # resumed meanwhile
            self.paused[ident] = requests
        try:
            thread = threading.current_thread().name
            self.reply({"paused": {"thread": thread, "id": ident, "frames": self.snapshot(frame, ident)}})
            while (ref := requests.get()) is not None:
                if ref in STEP_MODES:
                    self.step = Step(ident, frame, ref)
                    break
                _, value = self.refs.get(ref, (None, None))
                items = values.describe_all(values.children(value), self.make_ref(ident)) if value is not None else []
                self.reply({"children": {"ref": ref, "vars": items}})
        finally:
            del self.paused[ident]

    def make_ref(self, ident: int) -> Callable[[object], int]:
        def ref(value: object) -> int:
            number = next(self.next_ref)
            self.refs[number] = (ident, value)
            return number

        return ref

    def snapshot(self, frame, ident: int) -> list[dict]:
        """This thread's project frames, innermost first, with their variables."""
        ref = self.make_ref(ident)
        frames = []
        while frame is not None and len(frames) < MAX_PAUSED_FRAMES:
            code = frame.f_code
            node_id = self.node_id(code)
            if node_id:
                if code.co_qualname == "<module>":
                    items = values.describe_all(values.user_globals(frame.f_globals), ref)
                else:
                    items = values.describe_all(frame.f_locals.items(), ref)
                    module_data = dict(values.user_globals(frame.f_globals))
                    if module_data:
                        items.append({"name": "(globals)", "value": f"{len(module_data)} names", "ref": ref(module_data)})
                frames.append({"node": node_id, "name": code.co_qualname, "file": code.co_filename, "line": frame.f_lineno, "vars": items})
            frame = frame.f_back
        return frames

    # Bookkeeping, always in the thread the event happens in

    def enter(self, code, node_id: str, frame, new_call: bool) -> None:
        own = frame
        if frame.f_code is code:
            frame = frame.f_back
        caller_frame = self.project_frame(frame)
        ident = threading.get_ident()
        stack = self.thread_stacks.get(ident, [])
        if caller_frame is None and stack:
            # Called back from deeper than CALLER_SEARCH library frames (torch tracing a model, a test
            # run by pytest): look through the whole stack. Guessing the top of `stack` instead is wrong
            # when it holds a frame that left without an event.
            caller_frame = self.project_frame(frame, limit=None)
        caller = self.ids[caller_frame.f_code] if caller_frame else None
        origin = None
        if new_call:
            self.calls[node_id] = self.calls.get(node_id, 0) + 1
            if code.co_flags & ASYNC_FLAGS:
                # A task's own coroutine is called by the event loop: the project frame under the
                # loop (whoever called asyncio.run) is not its caller. Where it was created is.
                origin = self.task_origin(code)
                if origin:
                    self.count(self.spawns, f"{origin}>{node_id}")
                elif caller:
                    self.count(self.links, f"{caller}>{node_id}")
            else:
                if caller:
                    self.count(self.links, f"{caller}>{node_id}")
                origin = None if stack else getattr(threading.current_thread(), "_luciole_origin", None)
                if origin and origin != caller:  # first project code of a thread: who started it
                    self.count(self.spawns, f"{origin}>{node_id}")
                origin = None
        if caller in stack:  # cut what left without an event (an exception in library code, say)
            stack = stack[: len(stack) - stack[::-1].index(caller)]
        else:
            stack = [caller] if caller else []
        self.thread_stacks[ident] = [*stack, node_id]
        tracer = self.tracer
        if tracer is not None:
            if new_call:
                tracer.start(code, node_id, own, caller_frame, origin)
            else:
                tracer.resume(code, node_id, own)

    def resume(self, code, node_id: str, frame) -> None:
        if code.co_flags & ASYNC_FLAGS and self.waiting.get(node_id):
            self.waiting[node_id] -= 1
            if not self.waiting[node_id]:
                del self.waiting[node_id]
        self.enter(code, node_id, frame, new_call=False)

    def leave(self, node_id: str) -> None:
        ident = threading.get_ident()
        stack = self.thread_stacks.get(ident)
        if stack and stack[-1] == node_id:
            self.thread_stacks[ident] = stack[:-1]

    @staticmethod
    def count(counts: dict[str, int], key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    # Reporting, in the background thread

    def stacks(self) -> list[dict]:
        threads = {thread.ident: thread for thread in threading.enumerate()}
        main = threading.main_thread().ident
        result = []
        for ident, stack in sorted(list(self.thread_stacks.items()), key=lambda item: item[0] != main):
            if ident not in threads:
                self.thread_stacks.pop(ident, None)
            elif stack:
                result.append({"thread": threads[ident].name, "stack": stack})
        return result

    def flush(self) -> None:
        calls, self.calls = self.calls, {}
        links, self.links = self.links, {}
        spawns, self.spawns = self.spawns, {}
        state = (self.stacks(), dict(self.waiting))
        if calls or links or spawns or state != self.last_state:
            self.last_state = state
            t = round(time.perf_counter() - self.started, 3)
            stacks, waiting = state
            self.send({"t": t, "stacks": stacks, "calls": calls, "links": links, "spawns": spawns, "waiting": waiting})

    def thread_start_hook(self, start: Callable) -> Callable:
        """Wrap Thread.start to note, on the thread, the project function that started it."""
        recorder = self

        def luciole_start(thread, *args, **kwargs):
            origin = recorder.nearest_project(sys._getframe(1))
            if origin:
                thread._luciole_origin = origin
            if recorder.tracer is not None:
                thread._luciole_origin_frame = recorder.tracer.current_frame()
            return start(thread, *args, **kwargs)

        return luciole_start


def hide_recorder_frames(error: BaseException) -> None:
    """Cut the traceback at the program's last frame.

    Ctrl+C (or Stop in the view) raises KeyboardInterrupt wherever the main thread is, often inside
    a monitoring callback: the traceback should end at the program's line, not in the recorder.
    """
    # The traceback starts in run() (this file), goes through runpy into the program, and may end
    # in a callback (this file again) and whatever the callback calls.
    tb, previous, in_program = error.__traceback__, None, False
    while tb is not None:
        in_recorder = tb.tb_frame.f_code.co_filename == __file__
        if in_recorder and in_program:
            previous.tb_next = None
            return
        in_program = in_program or not in_recorder
        previous, tb = tb, tb.tb_next


class Session:
    """The recorder installed in this interpreter: its monitoring callbacks, the pump thread that
    sends a frame every FRAME seconds, and the control thread that reads the view's commands."""

    def __init__(self, root: Path, port: int | None, connection: socket.socket | None, events: Path | None, trace: Path | str | None = None, level: int = 0):
        self.port = port  # to connect again from a forked child
        self.connection = connection
        self.trace = trace  # trace file or directory (forked children write next to it)
        self.level = level
        self.file = events.open("w", encoding="utf-8") if events else None
        self.sinks: list[Callable[[str], None]] = []
        if connection:
            self.sinks.append(lambda line: connection.sendall(line.encode()))
        if self.file:
            self.sinks.append(lambda line: (self.file.write(line), self.file.flush()))
        self.view_sinks = self.sinks[:1] if connection else []
        # The pump and paused threads all write; reentrant for on_sigterm, which may interrupt a write.
        self.write_lock = threading.RLock()
        self.recorder = Recorder(root, self.send, lambda message: self.write(message, self.view_sinks))
        if trace:
            from luciole.tracer import Tracer

            self.recorder.tracer = Tracer(trace, level, self.recorder.root)
        self.stopping = threading.Event()
        self.tool: int | None = None
        self.thread_start = threading.Thread.start
        self.origin_depth = 0

    def write(self, frame: dict, targets: list) -> None:
        line = json.dumps(frame, separators=(",", ":")) + "\n"
        with self.write_lock:
            for sink in list(targets):
                try:
                    sink(line)
                except OSError:
                    targets.remove(sink)  # the view went away; keep running the program

    def send(self, frame: dict) -> None:
        self.write(frame, self.sinks)

    def start(self) -> None:
        """Install the callbacks. Raises ValueError when every free monitoring tool id is taken."""
        monitoring = sys.monitoring
        # Ids 3 and 4 have no reserved role: the debugger (debugpy), coverage and profiler (cProfile)
        # ids stay free for those tools, which must keep working with Luciole on.
        tool = next((tool for tool in (4, 3) if monitoring.get_tool(tool) is None), None)
        if tool is None:
            raise ValueError("no free sys.monitoring tool id")
        recorder = self.recorder
        if self.connection:
            # Before the pump thread starts: the engine refuses a connection that does not start
            # with a hello.
            hello = protocol.hello("recorder", pid=os.getpid(), argv=sys.orig_argv, root=str(recorder.root))
            self.write(hello, self.view_sinks)
        events = monitoring.events
        callbacks = {
            events.PY_START: recorder.on_start,
            events.PY_RESUME: recorder.on_resume,
            events.PY_THROW: recorder.on_throw,
            events.PY_YIELD: recorder.on_yield,
            events.PY_RETURN: recorder.on_return,
            events.PY_UNWIND: recorder.on_unwind,
            events.LINE: recorder.on_line,
        }
        if recorder.tracer is not None:
            callbacks[events.RAISE] = recorder.on_raise
            recorder.tracer.start_writer(own_thread)
        always = sum(callbacks) - events.LINE
        monitoring.use_tool_id(tool, "luciole")
        self.tool = tool
        self.events = list(callbacks)
        for event, callback in callbacks.items():
            monitoring.register_callback(tool, event, callback)
        recorder.line_events = lambda on: monitoring.set_events(tool, always | (events.LINE if on else 0))
        self.pump_thread = own_thread(self.pump, "luciole-recorder")
        self.pump_thread.start()
        self.thread_start = threading.Thread.start
        threading.Thread.start = recorder.thread_start_hook(self.thread_start)
        self.origin_depth = sys.get_coroutine_origin_tracking_depth()
        sys.set_coroutine_origin_tracking_depth(ORIGIN_DEPTH)
        monitoring.set_events(tool, always)
        if self.connection:
            # After set_events above, which would undo an early pause's LINE events.
            own_thread(self.control, "luciole-control").start()

    def stop(self, close: bool) -> None:
        """Remove the callbacks, send the last frame and {"done": true}; `close` the connection too.

        `luciole run` keeps it open: the view knows the run is over when the process exits.
        """
        self.recorder.line_events = lambda on: None
        self.recorder.resume_all()  # threads paused while the main code ended
        self.uninstall()
        self.stopping.set()
        self.pump_thread.join()
        self.recorder.flush()
        self.send({"done": True})
        self.close_trace("stop")
        if close:
            self.close()

    def terminate(self) -> None:
        """On SIGTERM, just before the process dies: send the last frame and {"done": true}, and
        write the end of the trace."""
        self.stopping.set()
        self.pump_thread.join(timeout=4 * FRAME)  # in vain if this thread holds the write lock
        self.recorder.flush()
        self.send({"done": True})
        self.close_trace("sigterm", timeout=0.5)

    def uninstall(self) -> None:
        monitoring = sys.monitoring
        monitoring.set_events(self.tool, 0)
        for event in self.events:
            monitoring.register_callback(self.tool, event, None)
        monitoring.free_tool_id(self.tool)
        if getattr(threading.Thread.start, "__name__", "") == "luciole_start":
            threading.Thread.start = self.thread_start
        sys.set_coroutine_origin_tracking_depth(self.origin_depth)

    def close(self) -> None:
        if self.connection:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)  # wakes the control thread
            except OSError:
                pass
            self.connection.close()
        if self.file:
            self.file.close()
        self.close_trace("stop")  # done already, unless the session never started

    def close_trace(self, ended_by: str, timeout: float | None = None) -> None:
        """Write the end of the trace, if any. Never raises: the program's own end comes first."""
        if self.recorder.tracer is not None:
            try:
                self.recorder.tracer.close(ended_by, timeout)
            except Exception as error:
                log.warning("could not end the trace %s: %s", self.recorder.tracer.path, error)

    def pump(self) -> None:
        while not self.stopping.wait(FRAME):
            self.recorder.flush()

    def control(self) -> None:
        """Commands from the view, one JSON object per line."""
        try:
            for line in self.connection.makefile(encoding="utf-8"):
                try:
                    message = json.loads(line)
                    if "refused" in message:  # an engine of another protocol: record for nobody
                        log.warning("the Luciole engine refused this recorder: %s", message["refused"])
                        break
                    self.recorder.command(message)
                except (ValueError, AttributeError) as error:
                    log.warning("ignored a command from the view: %s (%s)", line.strip()[:200], error)
        except (OSError, ValueError):  # ValueError: the connection was closed by stop()
            pass
        self.recorder.resume_all()  # never leave the program paused without a view


# The session of this interpreter, started by `luciole run`, luciole.start() or auto-start.
active: Session | None = None
ending: Session | None = None  # the session stop() is ending, which on_sigterm lets finish
terminated = False  # SIGTERM came while stop() was sending the last frame
hooks_installed = False


def connect(port: int | str | None, quiet: bool) -> socket.socket | None:
    """Connection to the engine on `port` (default: the running engine's), None when nothing listens."""
    port = port or protocol.engine_port()
    if not port:
        if not quiet:
            log.warning("no Luciole engine is running: open the Luciole view in VS Code, or run `luciole engine`")
        return None
    try:
        connection = socket.create_connection(("127.0.0.1", int(port)), timeout=2)
    except (OSError, ValueError) as error:
        if not quiet:
            log.warning("could not reach the Luciole view on port %s: %s", port, error)
        return None
    connection.settimeout(None)
    return connection


def trace_level(level: int | str | None) -> int:
    """The trace level asked for (default: LUCIOLE_TRACE_LEVEL, else 0)."""
    try:
        return int(level if level is not None else os.environ.get("LUCIOLE_TRACE_LEVEL") or 0)
    except ValueError:
        return 0


def start(
    root: Path | str | None = None,
    port: int | str | None = None,
    events: Path | str | None = None,
    quiet: bool = False,
    connection: socket.socket | None = None,
    trace: Path | str | None = None,
    level: int | None = None,
) -> bool:
    """Start recording this process, until stop() or the process exits. See luciole.start()."""
    global active
    if active is not None:
        return True
    if sys.version_info < (3, 12):
        if not quiet:
            log.warning("live mode needs Python 3.12 or newer (sys.monitoring); this is %s", sys.version.split()[0])
        return False
    trace = trace or os.environ.get("LUCIOLE_TRACE") or None
    if events is None and trace is None and "luciole" in map(sys.monitoring.get_tool, (3, 4)):
        return True  # another copy of luciole (auto-start's, see import_bundled) already reports it
    port = port or os.environ.get("LUCIOLE_LIVE_PORT") or protocol.engine_port()
    if connection is None and port:
        connection = connect(port, quiet)
    if connection is None and events is None and trace is None:
        return False  # nothing would see the events: record nothing
    root = Path(root or os.environ.get("LUCIOLE_ROOT") or Path.cwd())
    try:
        session = Session(root, int(port) if port else None, connection, Path(events) if events else None, trace, trace_level(level))
    except Exception as error:  # the trace file cannot be written
        if not quiet:
            log.warning("could not start the recorder: %s", error)
        if connection:
            connection.close()
        return False
    try:
        session.start()
    except ValueError as error:
        if not quiet:
            log.warning("could not start the recorder: %s", error)
        session.close()
        return False
    active = session
    install_hooks()
    install_sigterm()
    return True


def stop() -> None:
    """Stop recording this process. See luciole.stop()."""
    global active
    session, active = active, None
    if session is not None:
        end(session, close=True)


def end(session: Session, close: bool) -> None:
    """Stop `session`; a SIGTERM received meanwhile ends the process once its last frame is sent."""
    global ending
    ending = session
    try:
        session.stop(close)
    finally:
        ending = None
        if terminated:
            os.kill(os.getpid(), signal.SIGTERM)  # on_sigterm restored the default action


def install_hooks() -> None:
    """Once per process: stop at exit, and start again in forked children."""
    global hooks_installed
    if hooks_installed:
        return
    hooks_installed = True
    atexit.register(stop)
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=after_fork)


def install_sigterm() -> None:
    """Send the last calls when SIGTERM ends the process (multiprocessing's Pool terminates its
    workers): only when the program has no handler of its own, and the process then dies as it
    would have. Forked children inherit the handler, and report through their own session."""
    if os.name == "nt" or threading.current_thread() is not threading.main_thread():
        return  # Windows terminates processes without a signal handler; only the main thread can set one
    try:
        if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            signal.signal(signal.SIGTERM, on_sigterm)
    except (ValueError, OSError):
        pass


def on_sigterm(signum: int, frame) -> None:
    global active, terminated
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    if ending is not None:
        # A multiprocessing worker is often stopping already (os._exit after its last task) when
        # its pool terminates it, maybe interrupted halfway through its last frame: let it finish
        # (end() then kills the process), within a second.
        terminated = True
        _thread.start_new_thread(kill_later, (1,))
        return
    session, active = active, None
    if session is not None:
        # In a thread of its own, given a second at most: the main thread, interrupted by this
        # handler, may hold a lock it needs. The process must die whatever happens.
        done = _thread.allocate_lock()
        done.acquire()

        def terminate() -> None:
            try:
                session.terminate()
            except Exception:
                pass
            done.release()

        try:
            _thread.start_new_thread(terminate, ())  # not threading's: its lock may be held too
            done.acquire(timeout=1)
        except Exception:
            pass
    os.kill(os.getpid(), signal.SIGTERM)


def kill_later(seconds: float) -> None:
    time.sleep(seconds)
    os.kill(os.getpid(), signal.SIGTERM)


def after_fork() -> None:
    """In a forked child: drop the parent's session (its threads did not survive the fork, its
    connection is the parent's) and connect again as a process of its own."""
    global active
    session, active = active, None
    if session is None:
        return
    try:
        session.uninstall()
        if session.connection:
            session.connection.close()  # the child's copy: the parent's connection stays open
        trace = None
        if session.recorder.tracer is not None:
            from luciole.tracer import child_target

            session.recorder.tracer.forget()  # the parent's file: the child writes its own
            trace = child_target(session.trace)
        if (session.connection or trace) and start(session.recorder.root, session.port, quiet=True, trace=trace, level=session.level):
            exit_ = os._exit
            if exit_.__name__ != "luciole_exit":
                # Forked children usually end with os._exit (multiprocessing's do), which skips
                # atexit: send the last frame first.
                def luciole_exit(code: int) -> None:
                    try:
                        stop()
                    except Exception:
                        pass
                    exit_(code)

                os._exit = luciole_exit
    except Exception:  # never break the child
        pass


def run(target: list[str], as_module: bool, root: Path, port: int | None, events: Path | None, trace: Path | None = None, level: int = 0) -> int:
    """Run a script (or module, with `as_module`) under the recorder, writing a trace to `trace`
    (a file, or a directory to make one in) at `level`. Returns its exit code."""
    global active
    if sys.version_info < (3, 12):
        log.error("live mode needs Python 3.12 or newer (sys.monitoring); this is %s", sys.version.split()[0])
        return 2
    stop()  # auto-started in this interpreter: this run records it instead, with its own options
    # Without --port, the running engine's, if any: then nothing to warn about when there is none.
    session = Session(root, port, connect(port, quiet=not port), events, trace, level)
    session.start()
    active = session
    install_hooks()
    install_sigterm()
    sys.argv = list(target)
    status = None
    try:
        if as_module:
            runpy.run_module(target[0], run_name="__main__", alter_sys=True)
        else:
            sys.path.insert(0, str(Path(target[0]).resolve().parent))
            runpy.run_path(target[0], run_name="__main__")
        status = 0
    except SystemExit as exit:
        if exit.code is None or isinstance(exit.code, int):
            status = exit.code or 0
        else:
            print(exit.code, file=sys.stderr)
            status = 1
    except KeyboardInterrupt as interrupt:
        hide_recorder_frames(interrupt)
        raise
    except BaseException:
        status = 1  # the traceback is printed by Python, as without Luciole
        raise
    finally:
        if session.recorder.tracer is not None:
            session.recorder.tracer.exit_status = status
        if active is session:
            active = None
            end(session, close=False)
    return status
