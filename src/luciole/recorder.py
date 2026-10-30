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
The session that installs the recorder in an interpreter, and its start and stop, are in
session.py.

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

import inspect
import itertools
import logging
import queue
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from luciole import values
from luciole.analyzer import SKIP_DIRS, module_name

if TYPE_CHECKING:
    from luciole.tracer import Tracer  # imported when tracing only: sqlite3 costs a few ms

log = logging.getLogger(__name__)

FRAME = 0.05  # seconds between two frames
# Seconds to wait for the engine's answer to the hello, before the program goes on. The engine
# answers within a millisecond or so, and a loaded CI machine within tens of them; a port that
# something else now holds (an engine killed without removing its file) may never answer, and
# every process started with auto-start on would wait this long.
ANSWER_TIMEOUT = 0.5
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
                self.tracer.unwound(code, sys._getframe(1), exception)
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
        """LINE, on while pausing or stepping (project code stops here), and for the code of
        functions whose variables a trace records (level 2)."""
        tracer = self.tracer
        if tracer is not None and tracer.scope is not None and tracer.line(sys._getframe(1), line):
            if self.step is None and not self.pausing:
                return None  # scoped code, not paused: the common case, without node_id()'s lookup
        elif self.node_id(code) is None:
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
