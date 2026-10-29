"""Trace recording: each run's call tree, timings and exceptions (level 0), plus argument and
return values (level 1), written to a SQLite file (docs/trace-format.md, schema in trace.py).

The recorder's monitoring callbacks call the Tracer's hooks, in the thread where each event
happens. The hooks never touch SQLite. Each thread keeps its own stack of frames, one list per
frame (an "entry"), and appends each new entry to its own buffer; the frame's end is written
into the entry itself. One writer thread (the recorder's own, so debuggers neither list nor pause
it) drains the buffers every WRITE_EVERY seconds and inserts them in one transaction: an entry
that has ended by then is inserted once, complete, and the others are updated when they end.

A frame is one call: a generator or coroutine resumed many times stays one frame (found again
by its Python frame object), whose time runs from its first start to its end.
"""

from __future__ import annotations

import collections
import inspect
import itertools
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from luciole import __version__, trace, values

log = logging.getLogger(__name__)

WRITE_EVERY = 0.2  # seconds between two writes
MAX_FRAMES = 2_000_000  # frames stored per run; past it, frames are counted, not stored
MAX_SNAPSHOTS_CACHE = 100_000  # snapshots remembered by the writer to store each one once
RECENT_EXCEPTIONS = 64  # exceptions remembered per thread, to find what a new one is chained from
MAX_SUSPENDED = 100_000  # generators and coroutines remembered between two resumes
GENERATORS = inspect.CO_GENERATOR | inspect.CO_COROUTINE | inspect.CO_ITERABLE_COROUTINE | inspect.CO_ASYNC_GENERATOR
MESSAGE_CHARS = 500
FLOW = (GeneratorExit, StopIteration, StopAsyncIteration)  # control flow, not errors: not recorded

# An entry: a list, one per frame, with these fields. The thread fills the ones up to ARGS when
# the frame starts, then END_NS, EXCEPTION and RESULT when it ends, and KIND last.
FID, CODE, PYFRAME, NODE, CALLER, ORIGIN, THREAD, LINE, START_NS, ARGS, END_NS, KIND, EXCEPTION, RESULT = range(14)

# Other buffered events are tuples starting with one of these.
NEW_THREAD, RAISED, SEEN = range(3)

REDACTED = ("str", values.REDACTED, None, False)
IDENTITY = object()  # (IDENTITY, class, id): `<module.Class object at 0x…>`, formatted by the writer

# Connections of the parent, in a forked child: never used, never closed (closing would touch
# the parent's file).
abandoned: list = []


class ThreadTrace:
    """What one thread recorded: its stack of entries and its buffer of events."""

    __slots__ = ("row", "thread", "origin", "stack", "buffer", "recent")

    def __init__(self, row: int, thread: threading.Thread):
        self.row = row
        self.thread = thread  # to notice a new thread with the ident of a finished one
        self.origin = getattr(thread, "_luciole_origin_frame", None)  # the frame that started it
        self.stack: list[list] = []
        self.buffer: collections.deque = collections.deque()
        self.recent: dict[int, tuple] = {}  # id(exception) -> (row, type, frame id where raised)


def trace_file(target: Path | str) -> Path:
    """The file to write: `target` itself, or a new file in it when it is a directory (or ends
    with a separator): `trace-<date>-<time>-<pid>.sqlite`."""
    text = str(target)
    target = Path(target)
    if target.is_dir() or text.endswith(("/", os.sep)):
        return target / time.strftime(f"trace-%Y%m%d-%H%M%S-{os.getpid()}.sqlite")
    return target


def child_target(target: Path | str) -> Path:
    """Where a forked child writes: a new file in the same directory, `<name>-<pid>.sqlite`
    for a file target."""
    text = str(target)
    target = Path(target)
    if target.is_dir() or text.endswith(("/", os.sep)):
        return target
    return target.with_name(f"{target.stem}-{os.getpid()}{target.suffix}")


class Tracer:
    def __init__(self, target: Path | str, level: int, root: Path, max_frames: int | None = None):
        self.path = trace_file(target)
        self.level = level
        if max_frames is None:
            try:
                max_frames = int(os.environ.get("LUCIOLE_TRACE_MAX_FRAMES") or MAX_FRAMES)
            except ValueError:
                max_frames = MAX_FRAMES
        self.max_frames = max_frames
        self.started = time.perf_counter_ns()
        self.frame_ids = itertools.count(1)
        self.thread_ids = itertools.count(1)
        self.exception_ids = itertools.count(1)
        self.threads: dict[int, ThreadTrace] = {}  # thread ident -> its trace
        self.all_threads: list[ThreadTrace] = []  # for the writer
        self.suspended: dict[int, list] = {}  # id(Python frame) -> entry, for generators and coroutines
        self.arg_names: dict[object, tuple] = {}  # code -> ((name, secret), ...)
        self.exit_status: int | None = None
        self.writer = Writer(self, root)
        self.writer_thread: threading.Thread | None = None

    def start_writer(self, own_thread) -> None:
        self.writer_thread = own_thread(self.writer.loop, "luciole-trace")
        self.writer_thread.start()

    def now(self) -> int:
        return time.perf_counter_ns() - self.started

    # Hooks, called by the recorder's callbacks in the thread of the event

    def state(self) -> ThreadTrace:
        ident = threading.get_ident()
        state = self.threads.get(ident)
        if state is None or not state.stack and state.thread is not threading.current_thread():
            thread = threading.current_thread()
            state = ThreadTrace(next(self.thread_ids), thread)
            state.buffer.append((NEW_THREAD, state.row, ident, getattr(thread, "native_id", None), thread.name))
            self.threads[ident] = state
            self.all_threads.append(state)
        return state

    def start(self, code, node_id: str, frame, caller_frame, task_origin: str | None) -> None:
        """PY_START of project code: a new frame. `caller_frame` is the nearest project frame
        below it (None for none), `task_origin` the function that created a task's coroutine."""
        fid = next(self.frame_ids)
        state = self.state()
        stack = state.stack
        caller = line = origin = None
        if caller_frame is not None:
            key = id(caller_frame)
            index = len(stack) - 1
            while index >= 0 and stack[index][PYFRAME] != key:
                index -= 1
            if index >= 0:
                if index < len(stack) - 1:
                    self.lose(state, index + 1)
                caller = stack[index][FID]
                line = caller_frame.f_lineno
        elif stack:  # no project frame below: what the stack holds left without an event
            self.lose(state, 0)
        if task_origin:  # the creator, running or suspended (a coroutine gathering tasks)
            origin = next((entry[FID] for entry in reversed(stack) if entry[NODE] == task_origin), None)
            if origin is None:
                origin = max((entry[FID] for entry in list(self.suspended.values()) if entry[NODE] == task_origin), default=None)
        elif caller is None and state.origin:  # the thread's first frame: who started the thread
            origin, state.origin = state.origin, None
        args = self.arguments(code, frame) if self.level else None
        entry = [fid, code, id(frame), node_id, caller, origin, state.row, line, time.perf_counter_ns() - self.started, args, None, None, None, None]
        stack.append(entry)
        if code.co_flags & GENERATORS:
            suspended = self.suspended
            if len(suspended) >= MAX_SUSPENDED:  # abandoned generators end without an event
                del suspended[next(iter(suspended))]
            suspended[id(frame)] = entry
        if fid <= self.max_frames:
            state.buffer.append(entry)

    def resume(self, code, node_id: str, frame) -> None:
        """PY_RESUME or PY_THROW: a generator or coroutine continues, as the same frame."""
        entry = self.suspended.get(id(frame))
        if entry is None or entry[CODE] is not code:  # started before recording
            self.start(code, node_id, frame, None, None)
            return
        self.state().stack.append(entry)

    def suspend(self, code) -> None:
        """PY_YIELD: off the stack until it resumes."""
        state = self.threads.get(threading.get_ident())
        if state and state.stack and state.stack[-1][CODE] is code:
            state.stack.pop()[END_NS] = time.perf_counter_ns() - self.started  # in case it never resumes

    def end(self, code, kind: str, result=None, exception: int | None = None) -> None:
        """PY_RETURN, PY_UNWIND: the frame of `code` on top of this thread's stack ends."""
        state = self.threads.get(threading.get_ident())
        if not state or not state.stack:
            return  # started before recording
        stack = state.stack
        if stack[-1][CODE] is not code:
            index = len(stack) - 1
            while index >= 0 and stack[index][CODE] is not code:
                index -= 1
            if index < 0:
                return
            self.lose(state, index + 1)
        entry = stack.pop()
        if code.co_flags & GENERATORS:
            self.suspended.pop(entry[PYFRAME], None)
        entry[END_NS] = time.perf_counter_ns() - self.started
        entry[EXCEPTION] = exception
        if self.level and kind == "return":
            entry[RESULT] = self.value(result)
        entry[KIND] = kind  # last: the writer reads the end once this is set

    def returned(self, code, value) -> None:
        self.end(code, "return", value)

    def unwound(self, code, error: BaseException) -> None:
        """PY_UNWIND: the frame exits with `error`, raised there ("raise") or below ("unwind")."""
        state = self.threads.get(threading.get_ident())
        if not state or not state.stack:
            return
        if isinstance(error, GeneratorExit):
            self.end(code, "close")
            return
        known = state.recent.get(id(error))
        if known and known[1] is type(error):
            raised_here = known[2] == state.stack[-1][FID]
            self.end(code, "raise" if raised_here else "unwind", exception=known[0])
        else:
            self.end(code, "raise")

    def raised(self, code, frame, error: BaseException) -> None:
        """RAISE in project code: an exception is raised in `frame`, or reaches it from a call."""
        if isinstance(error, FLOW):
            return
        state = self.state()
        stack = state.stack
        fid = stack[-1][FID] if stack and stack[-1][PYFRAME] == id(frame) else None
        known = state.recent.get(id(error))
        tb = error.__traceback__
        if known and known[1] is type(error) and tb is not None and tb.tb_next is not None:
            if fid is not None and fid <= self.max_frames:
                state.buffer.append((SEEN, known[0], fid))  # passing through, maybe handled here
            return
        row = next(self.exception_ids)
        recent = state.recent
        if len(recent) >= RECENT_EXCEPTIONS:
            del recent[next(iter(recent))]
        recent[id(error)] = (row, type(error), fid)
        cause, chain = error.__cause__, "cause"
        if cause is None and not error.__suppress_context__:
            cause, chain = error.__context__, "context"
        cause_row = None
        if cause is not None:
            found = recent.get(id(cause))
            cause_row = found[0] if found and found[1] is type(cause) else None
        try:
            message = str(error)
        except Exception as failure:
            message = f"<str failed: {type(failure).__name__}>"
        if len(message) > MESSAGE_CHARS:
            message = message[: MESSAGE_CHARS - 1] + "…"
        kind = type(error)
        name = kind.__qualname__ if kind.__module__ == "builtins" else f"{kind.__module__}.{kind.__qualname__}"
        if fid is None or fid <= self.max_frames:
            event = (RAISED, row, fid, frame.f_lineno, name, message, cause_row, chain if cause_row else None, self.now())
            state.buffer.append(event)

    def current_frame(self) -> int | None:
        """This thread's innermost recorded frame (for threads it starts)."""
        state = self.threads.get(threading.get_ident())
        return state.stack[-1][FID] if state and state.stack else None

    def lose(self, state: ThreadTrace, index: int) -> None:
        """Frames above `index` left without an event (none was seen): end them as "lost"."""
        now = self.now()
        for entry in state.stack[index:]:
            self.suspended.pop(entry[PYFRAME], None)
            entry[END_NS] = now
            entry[KIND] = "lost"
        del state.stack[index:]

    # Level 1: values

    def arguments(self, code, frame) -> tuple | None:
        try:
            names = self.arg_names[code]
        except KeyError:
            count = code.co_argcount + code.co_kwonlyargcount
            count += bool(code.co_flags & inspect.CO_VARARGS) + bool(code.co_flags & inspect.CO_VARKEYWORDS)
            names = self.arg_names[code] = tuple((name, values.is_secret(name)) for name in code.co_varnames[:count])
        if not names:
            return ()
        try:
            local = frame.f_locals
            args = []
            for name, secret in names:
                try:
                    value = local[name]
                except KeyError:
                    continue
                if secret:
                    args.append((name, REDACTED))
                elif name == "self":  # its identity, without running its __repr__ (maybe not built yet)
                    args.append((name, (IDENTITY, type(value), id(value))))
                else:
                    args.append((name, self.value(value)))
            return tuple(args)
        except Exception:  # never break the program
            return None

    @staticmethod
    def value(value):
        """The value itself when immutable, or its identity when its class has no repr of its own
        (the writer snapshots those), else its snapshot now."""
        kind = type(value)
        if kind in values.IMMUTABLE:
            return value
        if kind.__repr__ is object.__repr__:
            return (IDENTITY, kind, id(value))
        try:
            return values.snapshot(value)
        except Exception as error:
            return (type(value).__qualname__, f"<snapshot failed: {type(error).__name__}>", None, True)

    # Ending

    def close(self, ended_by: str, timeout: float | None = None) -> None:
        """Write what is left and the run's end. Frames still on a stack end as "running"."""
        writer = self.writer
        writer.stopping.set()
        if self.writer_thread is not None:
            self.writer_thread.join(timeout)
        writer.finish(ended_by)

    def forget(self) -> None:
        """In a forked child: drop the parent's file without touching it."""
        self.writer.stopping.set()
        self.writer.finished = True
        abandoned.append(self.writer.db)


class Writer:
    """Inserts the buffered entries and events, in the writer thread (and once more at the end)."""

    def __init__(self, tracer: Tracer, root: Path):
        self.tracer = tracer
        self.db = trace.create(tracer.path)
        self.lock = threading.Lock()  # the writer thread and close() both write
        self.stopping = threading.Event()
        self.finished = False
        self.snapshot_ids: dict[tuple, int] = {}
        self.next_snapshot = itertools.count(1)
        self.open: list[list] = []  # entries inserted before they ended
        self.new_snapshots: list[tuple] = []
        self.frames_stored = 0
        argv = getattr(sys, "orig_argv", sys.argv)
        python = sys.version.split()[0]
        parent = os.getppid() if hasattr(os, "getppid") else None
        self.db.execute(
            "INSERT INTO runs (id, pid, parent_pid, argv, root, started_at, python, luciole, level) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (os.getpid(), parent, json.dumps(argv), str(root), time.time(), python, __version__, tracer.level),
        )

    def loop(self) -> None:
        while not self.stopping.wait(WRITE_EVERY):
            try:
                self.write()
            except Exception as error:  # a full disk, say: stop tracing, keep the program running
                log.warning("stopped writing the trace %s: %s", self.tracer.path, error)
                return

    def drain(self) -> list:
        events = []
        for state in list(self.tracer.all_threads):
            buffer = state.buffer
            for _ in range(len(buffer)):
                events.append(buffer.popleft())
        return events

    def write(self) -> None:
        with self.lock:
            if not self.finished:
                self.insert(self.drain())

    def insert(self, events: list) -> None:
        level = self.tracer.level
        frames, still_open, threads, raised, seen = [], [], [], {}, []
        for event in events:
            if type(event) is list:  # an entry
                kind = event[KIND]  # first: the rest of its end is written before it
                if kind is None:
                    still_open.append(event)
                    frames.append(self.row(event, None, level))
                else:
                    frames.append(self.row(event, kind, level))
            elif event[0] == RAISED:
                raised[event[1]] = [*event[1:], event[2]]  # handled in the frame raising it, so far
            elif event[0] == SEEN:
                seen.append(event[1:])
            else:
                threads.append(event[1:])
        updates = []
        for entry in self.open:
            kind = entry[KIND]
            if kind is None:
                still_open.append(entry)
            else:
                result = self.snapshot(entry[RESULT]) if level and kind == "return" else None
                updates.append((entry[END_NS], kind, entry[EXCEPTION], result, entry[FID]))
        self.open = still_open
        handled = []
        for row_id, fid in seen:
            if row_id in raised:
                raised[row_id][-1] = fid
            else:
                handled.append((fid, row_id))
        db = self.db
        db.execute("BEGIN")
        db.executemany("INSERT INTO threads VALUES (?, ?, ?, ?)", threads)
        db.executemany("INSERT INTO frames VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", frames)
        db.executemany("UPDATE frames SET end_ns = ?, end_kind = ?, exception = ?, result = ? WHERE id = ?", updates)
        db.executemany("INSERT INTO exceptions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", raised.values())
        db.executemany("UPDATE exceptions SET handled_in = ? WHERE id = ?", handled)
        if self.new_snapshots:
            db.executemany("INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)", self.new_snapshots)
            self.new_snapshots = []
        db.execute("COMMIT")
        self.frames_stored += len(frames)

    def row(self, entry: list, kind: str | None, level: int) -> tuple:
        args = entry[ARGS]
        if level:
            snapshot = self.snapshot
            args = "{" + ", ".join(f'"{name}": {snapshot(value)}' for name, value in args) + "}" if args is not None else None
        if kind is None:
            return (entry[FID], entry[NODE], entry[CALLER], entry[ORIGIN], entry[THREAD], entry[LINE], entry[START_NS], None, None, None, args, None)
        result = self.snapshot(entry[RESULT]) if level and kind == "return" else None
        return (entry[FID], entry[NODE], entry[CALLER], entry[ORIGIN], entry[THREAD], entry[LINE], entry[START_NS], entry[END_NS], kind, entry[EXCEPTION], args, result)

    def snapshot(self, value) -> int:
        """Id of the snapshot of `value` (taken now when the tracer left it for later), stored once."""
        if type(value) is tuple:  # taken by the tracer, or `self`
            if value[0] is IDENTITY:
                _, cls, address = value
                value = (cls.__qualname__, f"<{cls.__module__}.{cls.__qualname__} object at {address:#x}>", None, False)
            key = value
        else:  # immutable: equal values make the same snapshot
            key = (type(value), value)
        found = self.snapshot_ids.get(key)
        if found is None:
            if key is not value:
                value = values.snapshot(value)
            if len(self.snapshot_ids) >= MAX_SNAPSHOTS_CACHE:
                self.snapshot_ids.clear()
            found = self.snapshot_ids[key] = next(self.next_snapshot)
            self.new_snapshots.append((found, *value))
        return found

    def finish(self, ended_by: str) -> None:
        """The last write, frames still running, which exceptions went unhandled, the run's end."""
        tracer = self.tracer
        with self.lock:
            if self.finished:
                return
            self.insert(self.drain())
            total = next(tracer.frame_ids) - 1
            running = {id(entry) for state in list(tracer.all_threads) for entry in list(state.stack)}
            suspended = [(entry[END_NS], entry[FID]) for entry in self.open if id(entry) not in running and entry[KIND] is None and entry[END_NS] is not None]
            db = self.db
            db.execute("BEGIN")
            # Generators and coroutines left suspended (or abandoned): they end where they last stopped.
            db.executemany("UPDATE frames SET end_ns = ?, end_kind = 'suspended' WHERE id = ? AND end_kind IS NULL", suspended)
            db.execute("UPDATE frames SET end_kind = 'running' WHERE end_kind IS NULL")
            # An exception is handled in the last frame it reached, unless that frame exited with it.
            db.execute("UPDATE exceptions SET handled_in = NULL WHERE handled_in IN (SELECT id FROM frames WHERE exception = exceptions.id)")
            db.execute(
                "UPDATE runs SET ended_at = ?, duration_ns = ?, exit_status = ?, ended_by = ?, frames_total = ?, frames_stored = ?, truncated = ? WHERE id = 1",
                (time.time(), tracer.now(), tracer.exit_status, ended_by, total, self.frames_stored, int(total > self.frames_stored)),
            )
            db.execute("COMMIT")
            db.executescript(trace.INDEXES)
            db.execute("PRAGMA journal_mode = DELETE")  # one file again, for readers and copies
            db.close()
            self.finished = True
