"""`luciole mcp`: an MCP server (stdio) that lets an agent run programs under the recorder and
query what happened, along with the project's static graph.

An agent debugs from a recording, not by stepping: `run` executes a command (a script, pytest,
anything that starts Python) with auto-start on, so every Python process it starts writes a
trace at level 1 (calls with their arguments and return values, exceptions) into a run folder.
The other tools read those traces (trace.py) and the graph (analyzer.py). Answers are text, short
and paginated, meant for a model to read.

The transport is MCP's stdio one: JSON-RPC 2.0 messages, one per line, on stdin and stdout.
Standard library only, like the rest of the Python side.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from luciole import __version__
from luciole.runs import finish_run, new_run, processes_of, runs_folder
from luciole.trace import Trace, plural, render_calls, render_exceptions, render_history, render_summary, render_traceback, render_tree

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
OUTPUT_HEAD = 20  # lines of a program's output shown from its start...
OUTPUT_TAIL = 80  # ...and from its end
MAX_TEXT = 20_000  # characters per answer

INSTRUCTIONS = """\
Luciole records Python programs so you can debug from what actually happened instead of adding
prints and re-running. Use `run` to execute a command (a script, `pytest -x tests/test_x.py`, a
module) under the recorder: every call of project code is recorded with its arguments and return
value, and every exception with where it was raised and handled. Then ask: `exceptions` for what
went wrong, `traceback` for the stack it went through with every frame's local variables at that
moment (a post-mortem debugger's view, no re-run needed), `calls` for every call of a function
with its values, `call_tree` for who called what, `frame` for one call in detail. When a wrong
value was made inside a function, before anything failed, `run` again with `scope` naming the
suspect functions (level 2): `variable_history` then shows every assignment in them, line by line,
with the values. When a value is wrong, ask `origin` where it came from: it walks back through
the assignments, arguments, return values, globals and attributes that made it, across functions,
each with its line and value (exact lines for a local assigned in several places need its
function in a `scope`: it says when, and how). `find` and `neighbors` answer from the static graph: where a
function is, who calls it, what it calls, which variables it reads and writes, even for code that
did not run. Runs are kept: `runs` lists them, and every query takes a run id (default: the last).
"""

FRAME_ARG = {"type": "integer", "description": "A frame id (#N in other answers)."}
RUN_ARG = {"type": "string", "description": "A run id from `run` or `runs` (default: the last run)."}
PROCESS_ARG = {"type": "integer", "description": "A process id within the run (default: its first process)."}
PAGE_ARGS = {
    "limit": {"type": "integer", "description": "How many to show (default 20)."},
    "offset": {"type": "integer", "description": "How many to skip (default 0)."},
}

TOOLS = [
    {
        "name": "run",
        "description": "Run a command under Luciole's recorder (Python 3.12+) and record what happens: every call of project code with its arguments and return value, every exception. Returns the exit status, the output, and the exceptions that escaped. Child processes are recorded too. With `scope`, also every assignment to the local variables of those functions (for `variable_history`).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The command, as typed in a shell (no pipes): `python app.py --flag`, `pytest -x tests/test_cart.py`, `python -m pkg`."},
                "cwd": {"type": "string", "description": "Folder to run in (default: the project root)."},
                "level": {"type": "integer", "enum": [0, 1, 2], "description": "1 (default): arguments and return values too. 0: calls and exceptions only, cheaper. 2 (the default with a `scope`): level 1, plus every assignment in the `scope` functions."},
                "scope": {
                    "type": ["string", "array"],
                    "items": {"type": "string"},
                    "description": "Functions whose local variables to record, line by line (level 2): node ids or their ends (`cart:checkout`, `Cart.add`, `checkout`), `*` as a wildcard (`cart:Cart.*`), as a list or comma-separated. Keep it to the few suspect functions: each line they run costs a few microseconds.",
                },
                "timeout": {"type": "number", "description": "Seconds before the program is stopped (default 600)."},
            },
            "required": ["command"],
        },
    },
    {"name": "runs", "description": "The recorded runs of this project, newest first.", "inputSchema": {"type": "object", "properties": {}}},
    {
        "name": "summary",
        "description": "A run's process: its command, duration and exit status, and its top functions by calls and by time.",
        "inputSchema": {"type": "object", "properties": {"run": RUN_ARG, "process": PROCESS_ARG}},
    },
    {
        "name": "exceptions",
        "description": "Exceptions in a run, in the order they were raised: type, message, the function and line that raised it, what it was chained from, and where it was handled (or not).",
        "inputSchema": {"type": "object", "properties": {"run": RUN_ARG, "process": PROCESS_ARG, **PAGE_ARGS}},
    },
    {
        "name": "traceback",
        "description": "The frames an exception went through, innermost first: for each, the line it was at and its local variables at that moment, then where it was handled. Default: the last exception nothing handled.",
        "inputSchema": {"type": "object", "properties": {"exception": {"type": "integer", "description": "An exception id (!N in other answers)."}, "run": RUN_ARG, "process": PROCESS_ARG}},
    },
    {
        "name": "calls",
        "description": "Every call of a function in a run, in order: its arguments, return value or exception, duration and caller. `function` is a node id (`pkg.mod:Class.method`) or its end (`Class.method`, `method`).",
        "inputSchema": {"type": "object", "properties": {"function": {"type": "string"}, "run": RUN_ARG, "process": PROCESS_ARG, **PAGE_ARGS}, "required": ["function"]},
    },
    {
        "name": "call_tree",
        "description": "The call tree of a run (or below one frame), repeated calls collapsed into `×N`.",
        "inputSchema": {
            "type": "object",
            "properties": {"frame": FRAME_ARG, "depth": {"type": "integer", "description": "Levels shown (default 4)."}, "run": RUN_ARG, "process": PROCESS_ARG},
        },
    },
    {
        "name": "frame",
        "description": "One call in detail: its arguments, result or exception, caller, and the calls it made.",
        "inputSchema": {"type": "object", "properties": {"frame": FRAME_ARG, "run": RUN_ARG, "process": PROCESS_ARG}, "required": ["frame"]},
    },
    {
        "name": "variable_history",
        "description": "Every assignment to a function's local variables during a run, call by call and line by line, with the value assigned (rebinding, and changes to lists, dicts and sets): how a value was built. Needs a run made with `scope` covering the function (level 2); when it was not, says so: then `run` the command again with `scope: [\"<function>\"]`.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function": {"type": "string", "description": "A node id (`pkg.mod:Class.method`) or its end (`Class.method`, `method`)."},
                "variable": {"type": "string", "description": "One variable only."},
                "frame": FRAME_ARG,
                "run": RUN_ARG,
                "process": PROCESS_ARG,
                "limit": {"type": "integer", "description": "How many assignments to show (default 50)."},
                "offset": {"type": "integer", "description": "How many to skip (default 0)."},
            },
        },
    },
    {
        "name": "origin",
        "description": "Where a value came from. From one variable of one call, walks back through what made it, each step with its line, code and value: the assignment that set it, the caller that passed it (a parameter), the call that returned it, the last call that set a global or an attribute of the same object; then what fed those, across functions. Use it on a wrong value (a wrong result, a bad argument or local in `traceback`) rather than guessing. A local assigned on several lines needs its function in a `scope` (level 2) to know which line ran: the answer says so, and how to re-run. The data paths come from static analysis: calls through objects of unknown type, and changes made inside containers, can be missing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function": {"type": "string", "description": "A node id (`pkg.mod:Class.method`) or its end (`Class.method`, `method`). Not needed with `frame`."},
                "variable": {"type": "string", "description": "A local or parameter (`due`), `return` (what the call returned), an attribute of self (`self.total`), a global the function reads (`RATE`), or a variable's node id (`pkg.mod:Class#total`)."},
                "frame": {"type": "integer", "description": "Which call (#N in other answers; default: the function's last call)."},
                "at": {"type": "integer", "description": "The value at this moment, in ns since the run started, as given where a walk was cut (default: the call's end)."},
                "depth": {"type": "integer", "description": "Steps followed back (default 8)."},
                "run": RUN_ARG,
                "process": PROCESS_ARG,
            },
            "required": ["variable"],
        },
    },
    {
        "name": "find",
        "description": "Find functions and classes in the project's static graph by name: their node id, file and line.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "A name, or the end of a node id (`Cart.total`)."}}, "required": ["name"]},
    },
    {
        "name": "neighbors",
        "description": "From the static graph, around one function: who calls it, what it calls, its parameters and where their values come from, which globals and attributes it reads and writes, and where its return value goes.",
        "inputSchema": {"type": "object", "properties": {"function": {"type": "string"}}, "required": ["function"]},
    },
]


class ToolError(Exception):
    """An answer to show the agent as an error (bad arguments, nothing recorded)."""


class Server:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.runs_dir = runs_folder(self.root)
        self.graph: dict | None = None
        self.graph_stamp: tuple | None = None
        self.graph_index: tuple | None = None  # (graph, origins.Graph of it), for `origin`

    # JSON-RPC

    def serve(self, stdin=sys.stdin, stdout=sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self.write(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            reply = self.handle(message)
            if reply is not None:
                self.write(stdout, reply)

    @staticmethod
    def write(stdout, message: dict) -> None:
        stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        stdout.flush()

    def handle(self, message: dict) -> dict | None:
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            return None  # a notification (initialized, cancelled): nothing to answer
        try:
            result = self.dispatch(method, message.get("params") or {})
        except LookupError:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"unknown method: {method}"}}
        return {"jsonrpc": "2.0", "id": ident, "result": result}

    def dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            asked = params.get("protocolVersion")
            return {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "luciole", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments") or {}
            try:
                text, error = self.call(name, arguments), False
            except ToolError as problem:
                text, error = str(problem), True
            except Exception as problem:  # a bug here must not end the session
                text, error = f"luciole: {type(problem).__name__}: {problem}", True
            if len(text) > MAX_TEXT:
                text = text[:MAX_TEXT] + "\n... (cut: ask for less, with limit/offset or a smaller depth)"
            return {"content": [{"type": "text", "text": text}], "isError": error}
        raise LookupError(method)

    # Tools

    def call(self, name: str, args: dict) -> str:
        if name == "run":
            return self.run(args)
        if name == "runs":
            return self.list_runs()
        if name == "find":
            return self.find(required(args, "name"))
        if name == "neighbors":
            return self.neighbors(required(args, "function"))
        page = int(args.get("limit") or 20), int(args.get("offset") or 0)
        with self.trace(args.get("run"), args.get("process")) as trace:
            if name == "summary":
                return render_summary(trace.summary())
            if name == "traceback":
                exception = args.get("exception")
                found = trace.traceback(int(exception) if exception is not None else None)
                if found is None and exception is not None:
                    raise ToolError(f"no exception !{exception} in this process")
                return render_traceback(found)
            if name == "exceptions":
                return render_exceptions(trace.exceptions(*page)).replace("(--limit, --offset)", "(limit, offset)")
            if name == "calls":
                found = trace.calls(required(args, "function"), *page)
                if not found["nodes"]:
                    raise ToolError(f"no calls of {args['function']} in this run: it did not run, or the name is wrong (`find` looks it up)")
                return render_calls(found).replace("(--limit, --offset)", "(limit, offset)")
            if name == "call_tree":
                entries = trace.tree(args.get("frame"), int(args.get("depth") or 4))
                return render_tree(entries) or "no calls recorded"
            if name == "frame":
                return self.frame(trace, int(required(args, "frame")))
            if name == "variable_history":
                return self.history(trace, args)
            if name == "origin":
                return self.origin(trace, args)
        raise ToolError(f"unknown tool: {name}")

    def run(self, args: dict) -> str:
        command = shlex.split(required(args, "command"))
        assigned = {}  # `NAME=value command ...`, as a shell reads it
        while command and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", command[0]):
            name, _, value = command.pop(0).partition("=")
            assigned[name] = value
        if not command:
            raise ToolError("empty command")
        cwd = Path(args.get("cwd") or self.root)
        if not cwd.is_absolute():
            cwd = self.root / cwd
        from luciole.cli import auto_start_env, scoped_level
        from luciole.session import scope_patterns

        scope = scope_patterns(args.get("scope"))
        level, problem = scoped_level(int(args["level"]) if args.get("level") is not None else None, scope)
        if problem:
            raise ToolError(problem)
        level = 1 if level is None else level
        timeout = float(args.get("timeout") or 600)
        try:
            run_id, folder = new_run(self.root, command, cwd, level, scope, by="agent")
        except OSError as error:
            raise ToolError(f"could not make a run folder: {error}") from None
        env = dict(os.environ)
        for key, value in auto_start_env(None, self.root, False, folder, level, scope).items():
            if value is None:
                env.pop(key, None)  # no port: the recorders find the engine's, if VS Code runs one
            else:
                env[key] = value
        for key, value in assigned.items():
            # The recorder's folder stays first on PYTHONPATH: without it nothing is recorded.
            env[key] = f"{env[key]}{os.pathsep}{value}" if key == "PYTHONPATH" and env.get(key) else value
        began = time.monotonic()
        timed_out = False
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as error:
            raise ToolError(f"could not start {command[0]}: {error}") from None
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop(process)
            output, _ = process.communicate()
        elapsed = time.monotonic() - began
        (folder / "output.txt").write_bytes(output)
        finish_run(folder, None if timed_out else process.returncode)
        status = f"stopped after {timeout:g} s (timeout)" if timed_out else f"exit status {process.returncode}"
        lines = [f"run {run_id}: {shlex.join(command)}", f"  {status}, {elapsed:.2f} s"]
        if scope:
            lines.append(f"  variable history (level 2) for {', '.join(scope)}: see `variable_history`")
        lines += ["", "output:", indent(clip(output.decode(errors="replace")))]
        lines += [""] + self.recorded(folder)
        return "\n".join(lines)

    def recorded(self, folder: Path) -> list[str]:
        """What a run's traces hold: one line per process, and the exceptions nothing handled."""
        processes = processes_of(folder)
        if not processes:
            return [
                "nothing recorded: no Python 3.12+ process ran project code under this folder",
                f"  (the project root is {self.root}; Python older than 3.12 cannot be recorded)",
            ]
        lines = [f"recorded {plural(len(processes), 'process')}:"]
        uncaught, calls = [], 0
        for path in processes:
            with Trace(path) as trace:
                run = trace.run()
                frames = trace.db.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
                calls += frames
                lines.append(f"  pid {run['pid']}: {shlex.join(run['argv'])}  ({plural(frames, 'call')}, {exit_text(run)})")
                for e in trace.exceptions(1000)["exceptions"]:
                    if e["handled_in"] is None and e["frame"]:
                        uncaught.append((run["pid"], e))
        if not calls:
            lines.append(f"no project code ran (the project root is {self.root})")
        if uncaught:
            lines += ["", "exceptions nothing in the project handled (`traceback` shows each frame's locals):"]
            for pid, e in uncaught[:10]:
                where = f"{e['node']}:{e['line']} (#{e['frame']})"
                lines.append(f"  pid {pid}: {e['type']}: {e['message']}  raised in {where}")
        return lines

    def list_runs(self) -> str:
        runs = sorted(self.runs_dir.glob("*/command.json"), reverse=True) if self.runs_dir.is_dir() else []
        if not runs:
            return "no runs yet: `run` records one"
        lines = []
        for path in runs:
            meta = json.loads(path.read_text())
            processes = processes_of(path.parent)
            status = meta.get("exit_status")
            outcome = "timed out or still running" if status is None else f"exit status {status}"
            lines.append(f"{path.parent.name}  {shlex.join(meta['command'])}  ({outcome}, {plural(len(processes), 'process')} recorded)")
        return "\n".join(lines)

    def frame(self, trace: Trace, frame_id: int) -> str:
        frame = trace.frame(frame_id)
        if frame is None:
            raise ToolError(f"no frame #{frame_id} in this process")
        text = render_calls({"nodes": [frame["node"]], "total": 1, "calls": [frame]}).split("\n", 1)[1].strip()
        entries = trace.tree(frame_id, depth=1)
        children = entries[0]["children"] if entries else []
        if children:
            text += "\n  calls:\n" + render_tree(children, indent=2)
        if frame["exception"]:
            text += f"\n  exception !{frame['exception'].get('id')} (see `exceptions`)"
        return text

    def history(self, trace: Trace, args: dict) -> str:
        function, frame = args.get("function"), args.get("frame")
        if not function and frame is None:
            raise ToolError("missing argument: function (or frame)")
        limit, offset = int(args.get("limit") or 50), int(args.get("offset") or 0)
        found = trace.history(function, args.get("variable"), int(frame) if frame is not None else None, limit, offset)
        if not found["nodes"]:
            if frame is not None:
                raise ToolError(f"no frame #{frame} in this process")
            raise ToolError(f"no calls of {function} in this run: it did not run, or the name is wrong (`find` looks it up)")
        if not found["recorded"]:
            node = found["nodes"][0]
            had = f"its scope was {', '.join(found['scope'])}" if found["scope"] else f"it was recorded at level {trace.run()['level']}, without a scope"
            raise ToolError(
                f"no variable history for {', '.join(found['nodes'])} in this run ({had}; {plural(found['calls'], 'call')} recorded, see `calls`). "
                f'To record it, `run` the same command again with scope: ["{node}"] (level 2), then ask `variable_history` again.'
            )
        if args.get("variable") and not found["total"]:
            return f"{args['variable']} was never assigned in {', '.join(found['nodes'])} ({plural(found['calls'], 'call')}): check the name, or ask without `variable`"
        return render_history(found).replace("(--limit, --offset)", "(limit, offset)")

    def origin(self, trace: Trace, args: dict) -> str:
        from luciole.origins import OriginError, origin, render_origin
        from luciole.slicing import Graph

        function, frame, at = args.get("function"), args.get("frame"), args.get("at")
        if not function and frame is None:
            raise ToolError("missing argument: function (or frame)")
        graph = self.static_graph()
        if self.graph_index is None or self.graph_index[0] is not graph:
            self.graph_index = (graph, Graph(graph))
        try:
            found = origin(trace, self.graph_index[1], function, required(args, "variable"), int(frame) if frame is not None else None, int(at) if at is not None else None, int(args.get("depth") or 8))
        except OriginError as problem:
            raise ToolError(f"{problem} (`calls` lists a function's calls, `find` looks names up)") from None
        return render_origin(found, rerun="mcp")

    # Static graph

    def find(self, name: str) -> str:
        graph = self.static_graph()
        found = [node for node in graph["nodes"] if node["kind"] in ("function", "class") and matches(node["id"], name)]
        if not found:
            raise ToolError(f"nothing named {name} in the project")
        return "\n".join(f"{node['kind']} {node['id']}  {node['file']}:{node['loc']['line']}" for node in found[:50])

    def neighbors(self, name: str) -> str:
        graph = self.static_graph()
        nodes = {node["id"]: node for node in graph["nodes"]}
        found = [node for node in graph["nodes"] if node["kind"] == "function" and matches(node["id"], name)]
        if not found:
            raise ToolError(f"no function named {name} in the project (`find` looks names up)")
        if len(found) > 1:
            raise ToolError("several functions match, give one:\n" + "\n".join(f"  {node['id']}" for node in found[:30]))
        fn = found[0]["id"]
        own = {nid for nid, node in nodes.items() if node["kind"] == "variable" and node["parent"] == fn}

        def site(edge: dict) -> str:
            return ", ".join(f"line {s['line']}" for s in edge["sites"][:3])

        def flag(edge: dict) -> str:
            return " (by method name, a guess)" if edge.get("by_name") else " (as a callback)" if edge.get("callback") else ""

        sections: dict[str, list[str]] = {key: [] for key in ("called by", "calls", "parameters", "reads", "writes", "return value goes to")}
        for node_id in sorted(own):
            node = nodes[node_id]
            if node.get("role") == "param":
                sections["parameters"].append(node["name"])
        for edge in graph["edges"]:
            kind, source, target = edge["kind"], edge["source"], edge["target"]
            if kind == "calls" and target == fn:
                sections["called by"].append(f"{source}  ({site(edge)}){flag(edge)}")
            elif kind == "calls" and source == fn:
                sections["calls"].append(f"{target}  ({site(edge)}){flag(edge)}")
            elif kind == "passes_arg" and target in own:
                sections["parameters"].append(f"  {nodes[target]['name']} <- {source}  ({site(edge)})")
            elif kind == "reads" and target == fn and source not in own:
                sections["reads"].append(source)
            elif kind == "writes" and source == fn and target not in own:
                sections["writes"].append(f"{target}  ({site(edge)})")
            elif kind == "returns" and source == f"{fn}#return":
                sections["return value goes to"].append(f"{target}  ({site(edge)})")
        node = nodes[fn]
        lines = [f"function {fn}  {node['file']}:{node['loc']['line']}"]
        for title, items in sections.items():
            if items:
                lines += [f"{title}:"] + [f"  {item}" for item in items[:40]]
                if len(items) > 40:
                    lines.append(f"  ... {len(items) - 40} more")
        return "\n".join(lines)

    def static_graph(self) -> dict:
        """The project's graph, analyzed again when a Python file changed."""
        from luciole.analyzer import build_graph, python_files

        files = sorted(python_files(self.root))
        stamp = tuple((str(path), path.stat().st_mtime_ns) for path in files)
        if self.graph is None or stamp != self.graph_stamp:
            self.graph, self.graph_stamp = build_graph(self.root), stamp
        return self.graph

    # Runs

    def trace(self, run_id: str | None, pid: int | None) -> Trace:
        folders = sorted(path.parent for path in self.runs_dir.glob("*/command.json")) if self.runs_dir.is_dir() else []
        if not folders:
            raise ToolError("no runs yet: `run` records one")
        if run_id:
            folder = self.runs_dir / str(run_id)
            if folder not in folders:
                raise ToolError(f"no run {run_id} (`runs` lists them)")
        else:
            folder = folders[-1]
        processes = processes_of(folder)
        if not processes:
            raise ToolError(f"run {folder.name} recorded nothing")
        if pid is None:
            return Trace(processes[0])
        for path in processes:
            trace = Trace(path)
            if trace.run()["pid"] == int(pid):
                return trace
            trace.close()
        raise ToolError(f"no process {pid} in run {folder.name}")


def exit_text(run: dict) -> str:
    """How a recorded process ended. Auto-started recorders do not see the exit status: `run`
    gives the command's own."""
    if run["ended_at"] is None:
        return "still running, or killed"
    if run["exit_status"] is not None:
        return f"exit status {run['exit_status']}"
    return "ended by SIGTERM" if run["ended_by"] == "sigterm" else "finished"


def matches(node_id: str, name: str) -> bool:
    return node_id == name or node_id.endswith((f":{name}", f".{name}"))


def required(args: dict, key: str):
    if args.get(key) in (None, ""):
        raise ToolError(f"missing argument: {key}")
    return args[key]


def clip(text: str) -> str:
    lines = text.rstrip("\n").splitlines()
    if not lines:
        return "(none)"
    if len(lines) > OUTPUT_HEAD + OUTPUT_TAIL:
        lines = lines[:OUTPUT_HEAD] + [f"... {plural(len(lines) - OUTPUT_HEAD - OUTPUT_TAIL, 'line')} ..."] + lines[-OUTPUT_TAIL:]
    return "\n".join(line[:500] for line in lines)


def indent(text: str) -> str:
    return "\n".join(f"  {line}" for line in text.splitlines())


def stop(process: subprocess.Popen) -> None:
    """Interrupt a program and the processes it started (Ctrl+C), then kill them if they stay."""
    try:
        os.killpg(process.pid, signal.SIGINT)
        process.wait(5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, AttributeError):  # no process groups (Windows), or already gone
        process.kill()


def serve(root: Path) -> int:
    Server(root).serve()
    return 0
