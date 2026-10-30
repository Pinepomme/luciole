"""The recorder's session in this interpreter: started by `luciole run`, luciole.start() or auto-start.

A session starts with `luciole run`, with luciole.start() in the program, or by itself in any
Python process started with the auto-start folder on PYTHONPATH (see _autostart/sitecustomize.py).
Forked children connect again as processes of their own. It connects to the engine, installs the
Recorder (recorder.py), and sends its last frame however the program ends: return, exception,
SIGTERM, os._exit in a forked child.
"""

from __future__ import annotations

import _thread
import atexit
import json
import itertools
import logging
import os
import runpy
import select
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from luciole import protocol, recorder
from luciole.recorder import ANSWER_TIMEOUT, FRAME, ORIGIN_DEPTH, Recorder, own_thread

log = logging.getLogger(__name__)


def hide_recorder_frames(error: BaseException) -> None:
    """Cut the traceback at the program's last frame.

    Ctrl+C (or Stop in the view) raises KeyboardInterrupt wherever the main thread is, often inside
    a monitoring callback: the traceback should end at the program's line, not in the recorder.
    """
    # The traceback starts in run() (this file), goes through runpy into the program, and may end
    # in a callback (recorder.py) and whatever the callback calls.
    own = {__file__, recorder.__file__}
    tb, previous, in_program = error.__traceback__, None, False
    while tb is not None:
        in_recorder = tb.tb_frame.f_code.co_filename in own
        if in_recorder and in_program:
            previous.tb_next = None
            return
        in_program = in_program or not in_recorder
        previous, tb = tb, tb.tb_next


def program_traceback(tb):
    """The traceback from the program's first frame on, as `python prog.py` shows it: without
    Luciole's own frames (the command line, run()) and runpy's above the program's <module>."""
    start = tb
    while start is not None and start.tb_frame.f_code is not run.__code__:
        start = start.tb_next
    if start is None:
        return tb
    start = start.tb_next
    while start is not None and start.tb_frame.f_code.co_filename in (runpy.__file__, "<frozen runpy>"):
        start = start.tb_next
    return start or tb


def show_from_program(error: BaseException) -> None:
    """Have Python's report of an uncaught exception (sys.excepthook, the program's own if it set
    one) start the traceback of `error` at the program's first frame. The frames above are added
    while the exception unwinds out of run(), so they can only be cut when it is printed."""
    previous = sys.excepthook

    def excepthook(kind, value, tb):
        sys.excepthook = previous
        if value is error and tb is not None:
            tb = program_traceback(tb)
            value = value.with_traceback(tb)
        previous(kind, value, tb)

    sys.excepthook = excepthook


class Session:
    """The recorder installed in this interpreter: its monitoring callbacks, the pump thread that
    sends a frame every FRAME seconds, and the control thread that reads the view's commands."""

    def __init__(self, root: Path, port: int | None, connection: socket.socket | None, events: Path | None, trace: Path | str | None = None, level: int = 0, scope: list[str] | None = None):
        self.port = port  # to connect again from a forked child
        self.connection = connection
        self.trace = trace  # trace file or directory (forked children write next to it)
        self.level = level
        self.scope = scope  # level 2: whose variables are recorded
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

            self.recorder.tracer = Tracer(trace, level, self.recorder.root, scope=scope)
        self.stopping = threading.Event()
        self.tool: int | None = None
        self.thread_start = threading.Thread.start
        self.origin_depth = 0
        self.answer = ""  # the engine's answer to the hello, for the control thread

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
            self.lines = self.connection.makefile(encoding="utf-8")
            self.answer = self.read_answer()
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
            # Level 2: LINE events for scoped code only, turned on as each first runs.
            recorder.tracer.watch = lambda code: monitoring.set_local_events(tool, code, events.LINE)
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
        tracer = self.recorder.tracer
        if tracer is not None:
            tracer.watch = None
            for code in tracer.watched:  # free_tool_id() leaves them on, until 3.14's clear_tool_id()
                monitoring.set_local_events(self.tool, code, 0)
            tracer.watched = []
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

    def read_answer(self) -> str:
        """The engine's welcome (or refusal), read now: a process that ended before its control
        thread read it would close a socket holding unread data, which resets the connection,
        and the engine would lose the calls not read yet (all of them, for a short process).

        Waits ANSWER_TIMEOUT at most, with select() rather than a socket timeout: a read that
        times out leaves the socket's file unreadable for good. With no answer in time, the
        program goes on and the control thread reads the answer, and any commands, later; only
        a process ending before then can still lose its calls that way."""
        try:
            ready, _, _ = select.select([self.connection], [], [], ANSWER_TIMEOUT)
            return self.lines.readline() if ready else ""  # the engine sends whole lines at once
        except (OSError, ValueError):
            return ""

    def control(self) -> None:
        """Commands from the view, one JSON object per line."""
        try:
            for line in itertools.chain([self.answer] if self.answer else [], self.lines):
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


def trace_scope(scope: str | list[str] | None) -> list[str]:
    """The level 2 scope asked for: node id patterns (default: LUCIOLE_TRACE_SCOPE, comma-separated)."""
    return scope_patterns(scope if scope is not None else os.environ.get("LUCIOLE_TRACE_SCOPE"))


def scope_patterns(scope: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Scope patterns from a list, or from a comma-separated string (`--scope`, LUCIOLE_TRACE_SCOPE).
    Matched against node ids by trace.in_scope(). Here, not in trace.py: auto-start cannot import
    more of luciole once the program runs (see _autostart/sitecustomize.py, import_bundled)."""
    if not scope:
        return []
    items = scope.split(",") if isinstance(scope, str) else [part for item in scope for part in str(item).split(",")]
    return [item.strip() for item in items if item.strip()]


def start(
    root: Path | str | None = None,
    port: int | str | None = None,
    events: Path | str | None = None,
    quiet: bool = False,
    connection: socket.socket | None = None,
    trace: Path | str | None = None,
    level: int | None = None,
    scope: str | list[str] | None = None,
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
    level, scope = trace_level(level), trace_scope(scope)
    if trace and level >= 2 and not scope:
        if not quiet:
            log.warning("trace level 2 records the variables of chosen functions only: give a scope (LUCIOLE_TRACE_SCOPE); recording at level 1")
        level = 1
    try:
        session = Session(root, int(port) if port else None, connection, Path(events) if events else None, trace, level, scope)
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
    session = active
    if session is not None:
        end(session, close=True)


def end(session: Session, close: bool) -> None:
    """Stop `session`; a SIGTERM received meanwhile ends the process once its last frame is sent.

    `ending` holds the session before `active` lets go of it: a SIGTERM in between (a pool
    terminating a worker that is calling os._exit) must find it in one or the other, or the
    process dies without sending its calls."""
    global active, ending
    ending = session
    if active is session:
        active = None
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
        if (session.connection or trace) and start(session.recorder.root, session.port, quiet=True, trace=trace, level=session.level, scope=session.scope or []):
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


def run(target: list[str], as_module: bool, root: Path, port: int | None, events: Path | None, trace: Path | str | None = None, level: int = 0, scope: list[str] | None = None) -> int:
    """Run a script (or module, with `as_module`) under the recorder, writing a trace to `trace`
    (a file, or a directory to make one in) at `level`, level 2 recording the variables of the
    functions in `scope`. Returns its exit code."""
    global active
    if sys.version_info < (3, 12):
        log.error("live mode needs Python 3.12 or newer (sys.monitoring); this is %s", sys.version.split()[0])
        return 2
    stop()  # auto-started in this interpreter: this run records it instead, with its own options
    if trace and level >= 2 and not scope:  # LUCIOLE_TRACE_LEVEL=2 alone (the options are checked)
        log.warning("trace level 2 records the variables of chosen functions only: give a scope (--scope); recording at level 1")
        level = 1
    # Without --port, the running engine's, if any: then nothing to warn about when there is none.
    connection = connect(port, quiet=not port)
    try:
        session = Session(root, port, connection, events, trace, level, scope)
    except Exception as error:  # the trace file cannot be written (on Windows, open in a reader)
        log.error("could not write the trace %s: %s", trace, error)
        if connection:
            connection.close()
        return 1
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
        show_from_program(interrupt)
        raise
    except BaseException as error:
        status = 1  # the traceback is printed by Python, as without Luciole
        show_from_program(error)
        raise
    finally:
        if session.recorder.tracer is not None:
            session.recorder.tracer.exit_status = status
        if active is session:
            end(session, close=False)
    return status
