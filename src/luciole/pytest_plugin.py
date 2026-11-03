"""pytest plugin: record a test run, and keep the recording when tests fail (post-mortem).

    pytest --luciole [--luciole-level 0|1|2] [--luciole-scope F,G] [--luciole-dir DIR] [--luciole-keep failed|always]

Installed with luciole (a `pytest11` entry point), and inert unless `--luciole` is given or
LUCIOLE_PYTEST=1 is set: installing luciole never changes anyone's test runs. When on, the run
is recorded at level 1 (docs/trace-format.md) into a run folder (runs.py, `by: pytest`): the
project's default one, or one in `--luciole-dir` (a CI artifact). Child processes are recorded
too, by auto-start (pytest-xdist workers, subprocesses): this process's environment turns it on
for them. Each test's outcome goes in the trace of the process that ran it (the `tests` table),
with its function's frame. At the end, a run where every test passed is deleted, unless
`--luciole-keep=always`, and one line says where the recording is and how to ask about it.

Under `luciole run --trace`, or with auto-start writing traces (LUCIOLE_TRACE), a recorder already
runs: its trace gets the outcomes, and nothing else changes (a pytest-xdist worker is such a
process). The MCP server's `run` sets LUCIOLE_PYTEST=1, so pytest runs an agent starts record
their outcomes that way, in the run folder the server made. A live-only recorder (the VS Code switch, no trace) is replaced by one that also traces.

pytest is imported here only: pytest loads this module, and the rest of luciole never does.
"""

from __future__ import annotations

import inspect
import os
import shutil
import sys
from pathlib import Path

import pytest

KEEP = ("failed", "always")
PASSED = (0, 5)  # pytest's exit statuses when nothing failed: OK, no tests collected


def pytest_addoption(parser) -> None:
    group = parser.getgroup("luciole", "Luciole: record the run (Python 3.12+), keep the recording when tests fail")
    group.addoption("--luciole", action="store_true", default=False, help="record this run with Luciole (also LUCIOLE_PYTEST=1)")
    group.addoption("--luciole-level", type=int, choices=(0, 1, 2), default=None, help="trace level: 0 calls and exceptions; 1 also values and failure locals (default); 2 also every assignment in --luciole-scope")
    group.addoption("--luciole-scope", default=None, help="level 2: functions whose variables to record, comma-separated (cart:checkout,Cart.*); implies --luciole-level 2")
    group.addoption("--luciole-dir", default=None, help="keep the recording in a run folder in DIR (a CI artifact) rather than ~/.luciole/runs")
    group.addoption("--luciole-keep", choices=KEEP, default="failed", help="failed (default): delete the recording when every test passed; always: keep it")


def enabled(config) -> bool:
    return bool(config.getoption("luciole")) or os.environ.get("LUCIOLE_PYTEST", "") not in ("", "0")


def pytest_configure(config) -> None:
    if enabled(config):
        recording = Recording(config)
        config.pluginmanager.register(recording, "luciole-recording")
        recording.start()


class Recording:
    """One pytest session's recording: started at configure, ended at sessionfinish."""

    def __init__(self, config):
        self.config = config
        self.worker = hasattr(config, "workerinput")  # a pytest-xdist worker
        self.recorder = None  # the session's, to name test functions
        self.tracer = None  # where outcomes go; None when this process records nothing
        self.folder: Path | None = None  # the run folder, when this session made it
        self.runs_dir: Path | None = None  # --luciole-dir's folder
        self.saved_env: dict[str, str | None] = {}
        self.header: str | None = None
        self.last_line: str | None = None
        self.attempts: dict[str, int] = {}  # nodeid -> attempts started (reruns of flaky tests)
        self.pending: dict[str, dict] = {}  # nodeid -> the attempt running
        self.ended = False

    # Starting

    def start(self) -> None:
        if sys.version_info < (3, 12):
            self.header = f"luciole: recording needs Python 3.12 or newer (sys.monitoring); this is {sys.version.split()[0]}: nothing recorded"
            return
        from luciole import session as sessions

        running = sessions.current()
        tracer = running.recorder.tracer if running is not None else None
        if tracer is not None:
            # luciole run --trace, or auto-start with LUCIOLE_TRACE: add the outcomes to its trace.
            if hasattr(tracer, "test"):  # an older luciole's auto-start has no tests table
                self.recorder, self.tracer = running.recorder, tracer
                self.header = f"luciole: test outcomes go to the trace being recorded, {tracer.path}"
            return
        if self.worker:
            return  # the controller's environment turned auto-start on here: it did not start
        if running is None and os.environ.get("LUCIOLE_TRACE"):
            # Auto-start was to record this process (the MCP server's `run`, Run Live) and did not
            # start: a run folder is already made for it, never make a second one.
            self.header = "luciole: auto-start did not record this process (LUCIOLE_TRACE is set): no test outcomes recorded"
            return
        self.own(sessions, running)

    def own(self, sessions, running) -> None:
        """Record into a run folder of this session's own."""
        from luciole.cli import auto_start_env, scoped_level
        from luciole.runs import new_run

        config = self.config
        scope = sessions.scope_patterns(config.getoption("luciole_scope"))
        level, problem = scoped_level(config.getoption("luciole_level"), scope)
        if problem:
            raise pytest.UsageError(f"--luciole: {problem}")
        level = 1 if level is None else level
        root = Path(config.rootpath).resolve()
        directory = config.getoption("luciole_dir")
        self.runs_dir = (Path(config.invocation_params.dir) / directory).resolve() if directory else None
        command = ["pytest", *map(str, config.invocation_params.args)]
        try:
            _, self.folder = new_run(root, command, Path(config.invocation_params.dir).resolve(), level, scope, by="pytest", runs_dir=self.runs_dir)
        except OSError as error:
            self.header = f"luciole: could not make a run folder: {error}: nothing recorded"
            return
        if running is not None:  # a live-only recorder (no trace): this one traces, and lights the view up too
            stop_session(running)
        port = os.environ.get("LUCIOLE_LIVE_PORT")
        for name, value in auto_start_env(int(port) if port else None, root, False, self.folder, level, scope).items():
            self.saved_env[name] = os.environ.get(name)
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.header = f"luciole: recording at level {level} into {self.folder}"
        if getattr(config.option, "dist", "no") not in ("no", None):
            return  # the pytest-xdist controller runs no tests: its workers record, by auto-start
        if sessions.start(root, trace=str(self.folder) + os.sep, level=level, scope=scope, quiet=True) and sessions.active is not None:
            self.recorder, self.tracer = sessions.active.recorder, sessions.active.recorder.tracer
        else:
            self.header += " (this process could not be recorded: is another tool using sys.monitoring?)"

    # Outcomes

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item) -> None:
        if self.tracer is None:
            return
        if item.nodeid in self.pending:  # an attempt that ended without its teardown report (a rerun)
            self.record(item.nodeid)
        self.begin(item.nodeid, item)

    def begin(self, nodeid: str, item=None) -> dict:
        attempt = self.attempts[nodeid] = self.attempts.get(nodeid, 0) + 1
        function = inspect.unwrap(getattr(item, "obj", None)) if item is not None else None
        code = getattr(function, "__code__", None)
        node = self.recorder.node_id(code) if code is not None else None
        state = self.pending[nodeid] = {"node": node, "attempt": attempt, "start": self.tracer.now(), "outcome": None, "phase": "call", "message": None}
        return state

    def pytest_runtest_logreport(self, report) -> None:
        if self.tracer is None or getattr(report, "node", None) is not None:
            return  # a worker's report, in the pytest-xdist controller: the worker records it
        state = self.pending.get(report.nodeid) or self.begin(report.nodeid)
        xfail = hasattr(report, "wasxfail")
        outcome = None
        if report.when == "call":
            if report.passed:
                outcome = "xpass" if xfail else "passed"
            elif report.skipped:
                outcome = "xfail" if xfail else "skipped"
            else:
                outcome = "failed"  # failed, or `rerun` (pytest-rerunfailures)
        elif report.skipped:
            outcome = "xfail" if xfail else "skipped"
        elif not report.passed and state["outcome"] not in ("failed", "error"):
            outcome = "error"  # in a fixture: setup or teardown
        if outcome is not None:
            state.update(outcome=outcome, phase=report.when, message=message_of(report))
        if report.when == "teardown" or report.outcome == "rerun":
            self.record(report.nodeid)

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        if nodeid in self.pending:
            self.record(nodeid)

    def record(self, nodeid: str) -> None:
        state = self.pending.pop(nodeid)
        try:
            self.tracer.test(nodeid, state["node"], state["outcome"] or "passed", state["phase"], state["attempt"], state["start"], self.tracer.now(), state["message"])
        except Exception:  # never break the test run
            pass

    # Ending

    def pytest_report_header(self, config):
        return self.header

    @pytest.hookimpl(trylast=True)  # after pytest-xdist has stopped its workers
    def pytest_sessionfinish(self, session, exitstatus) -> None:
        self.end(int(exitstatus))

    def end(self, status: int | None) -> None:
        if self.ended:
            return
        self.ended = True
        for nodeid in list(self.pending):
            self.record(nodeid)
        from luciole import session as sessions

        if self.folder is None:
            if self.tracer is not None and not self.worker:
                self.last_line = f"luciole: test outcomes recorded in {self.tracer.path}"
            return
        if self.tracer is not None and sessions.active is not None and sessions.active.recorder.tracer is self.tracer:
            self.tracer.exit_status = status
            sessions.stop()  # the trace is complete before it is kept or deleted
        self.restore_env()
        from luciole.runs import finish_run

        try:
            finish_run(self.folder, status)
        except (OSError, ValueError):
            pass
        if self.config.getoption("luciole_keep") == "failed" and status in PASSED:
            shutil.rmtree(self.folder, ignore_errors=True)
            self.last_line = "luciole: every test passed: recording deleted (--luciole-keep=always keeps it)"
            return
        agents = f"luciole mcp --runs {self.runs_dir} --no-run" if self.runs_dir else "luciole mcp, tool `tests`"
        self.last_line = f"luciole: recording kept in {self.folder}: luciole trace {self.folder} tests --failed; for an agent: {agents}"

    def pytest_terminal_summary(self, terminalreporter) -> None:
        if self.last_line:
            terminalreporter.write_line(self.last_line)
            self.last_line = None

    def pytest_unconfigure(self, config) -> None:
        self.end(None)  # when the session never finished (a usage error, say)
        self.restore_env()
        if self.last_line:
            print(self.last_line, file=sys.stderr)

    def restore_env(self) -> None:
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.saved_env = {}


def stop_session(session) -> None:
    """Stop a session, whichever copy of luciole started it: the stop() of its own module, which
    also forgets it (auto-start's copy is not in sys.modules)."""
    try:
        session.stop.__func__.__globals__["stop"]()
    except Exception:
        pass


def message_of(report) -> str | None:
    """A report's short message: the exception's for a failure, the reason for a skip or xfail."""
    longrepr = report.longrepr
    if longrepr is None:
        return getattr(report, "wasxfail", None) or None
    if isinstance(longrepr, tuple) and len(longrepr) == 3:  # a skip: (file, line, reason)
        return str(longrepr[2])
    crash = getattr(longrepr, "reprcrash", None)
    if crash is not None and crash.message:
        return crash.message
    lines = str(longrepr).strip().splitlines()
    return lines[-1] if lines else None
