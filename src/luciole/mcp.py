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
from luciole.mcp_tools import INSTRUCTIONS, POST_MORTEM, TOOLS, no_run_tools
from luciole.runs import finish_run, new_run, processes_of, runs_folder
from luciole.trace import Trace, fold_small
from luciole.trace_text import cut_short, first_line, plural, render_calls, render_exceptions, render_focus, render_history, render_summary, render_traceback, render_tree

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
RUN_TEXT = 3_500  # characters of `run`'s answer: the program's output is cut to fit
OUTPUT_HEAD = 0.3  # the share of the output's room given to its start; its end gets the rest
LINE_CHARS = 300  # characters of a line of output, or of a process's command line
MAX_TEXT = 20_000  # characters per answer
# Commands that run a program in an environment they may first build or sync: minutes of
# installing, and output that buries the program's. Their options that take a value.
WRAPPERS = {"uv", "poetry", "pipenv", "hatch", "pdm", "conda", "rye", "uvx", "pixi"}
WRAPPER_OPTIONS = {"--with", "--python", "-p", "--env-file", "-n", "--name", "-e", "--env", "--environment", "--prefix", "--package", "--extra", "--group", "--directory", "--project", "--from", "--index"}


class ToolError(Exception):
    """An answer to show the agent as an error (bad arguments, nothing recorded)."""


class Server:
    def __init__(self, root: Path, runs_dir: Path | None = None, allow_run: bool = True):
        self.root = root.resolve()
        if runs_dir is not None and (runs_dir / "command.json").is_file():
            runs_dir = runs_dir.parent  # one run's folder: the folder holding it
        self.runs_dir = runs_dir.resolve() if runs_dir is not None else runs_folder(self.root)
        self.allow_run = allow_run  # False: post-mortem, existing recordings only
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
                "instructions": INSTRUCTIONS if self.allow_run else POST_MORTEM,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS if self.allow_run else no_run_tools()}
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
            if not self.allow_run:
                raise ToolError("this server reads existing recordings only (luciole mcp --no-run): it cannot run programs")
            return self.run(args)
        if name == "tests":
            return self.tests(args)
        if name == "suspects":
            return self.suspects(args)
        if name == "compare":
            return self.compare(args)
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
                found = trace.traceback(int(exception) if exception is not None else None, original=True)
                if found is None and exception is not None:
                    raise ToolError(f"no exception !{exception} in this process")
                text = render_traceback(found)
                if found and found["frames"]:
                    frame = found["frames"][0]
                    if ":" in frame["node"]:  # a function, not a module's body
                        text += f"\nnext: `calls` function={frame['node']} shows who called it, with which values; `origin` frame={frame['frame']} variable=NAME, where a wrong value came from"
                    else:
                        text += f"\nnext: `origin` frame={frame['frame']} variable=NAME shows where a wrong value came from"
                return text
            if name == "exceptions":
                return render_exceptions(trace.exceptions(*page)).replace("(--limit, --offset)", "(limit, offset)")
            if name == "calls":
                return self.calls(trace, args, page)
            if name == "call_tree":
                return self.call_tree(trace, args)
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
        wrapper = Path(command[0]).name
        if wrapper in WRAPPERS and (wrapper == "uvx" or command[1:2] == ["run"]):
            raise ToolError(self.unwrap(command))
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
            run_id, folder = new_run(self.root, command, cwd, level, scope, by="agent", runs_dir=self.runs_dir)
        except OSError as error:
            raise ToolError(f"could not make a run folder: {error}") from None
        env = dict(os.environ)
        # pytest records each test's outcome in the trace auto-start writes (the plugin, when
        # luciole is installed where pytest runs; inert for anything else).
        env["LUCIOLE_PYTEST"] = "1"
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
        except FileNotFoundError:
            finish_run(folder, None)
            raise ToolError(self.not_found(command, cwd)) from None
        except OSError as error:
            finish_run(folder, None)
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
        lines = [f"run {run_id}: {cut(shlex.join(command), LINE_CHARS)}", f"  {status}, {elapsed:.2f} s"]
        if scope:
            lines.append(f"  variable history (level 2) for {', '.join(scope)}: see `variable_history`")
        recorded = self.recorded(folder)
        room = RUN_TEXT - sum(len(line) + 1 for line in lines + recorded) - 20
        lines += ["", "output:", indent(clip(output.decode(errors="replace"), max(room, 800), folder / "output.txt"))]
        lines += [""] + recorded
        return "\n".join(lines)

    def unwrap(self, command: list[str]) -> str:
        """Why `uv run pytest` is not run, and the command to run instead."""
        rest = command[1:] if Path(command[0]).name == "uvx" else command[2:]
        while rest and rest[0].startswith("-"):
            option = rest.pop(0)
            if "=" not in option and option in WRAPPER_OPTIONS and rest:
                rest.pop(0)
        wrapper = shlex.join(command[:1] if Path(command[0]).name == "uvx" else command[:2])
        python = next((path for path in (self.root / ".venv" / "bin" / "python", self.root / ".venv" / "Scripts" / "python.exe") if path.exists()), None)
        if rest and python is not None:
            program = Path(rest[0]).name
            if re.fullmatch(r"python[\d.]*(\.exe)?", program):
                rest = [str(python.relative_to(self.root)), *rest[1:]]
            elif program in ("pytest", "py.test"):
                rest = [str(python.relative_to(self.root)), "-m", "pytest", *rest[1:]]
        instead = f"`{shlex.join(rest)}`" if rest else "the program with the project's interpreter"
        return (
            f"not run: `{wrapper}` may build or sync an environment first (minutes of installing, and output that buries the program's). "
            f"Run {instead} instead, with the interpreter of the environment that is already set up (Python 3.12+ to be recorded)."
        )

    def not_found(self, command: list[str], cwd: Path) -> str:
        """`cfn-lint` is not on PATH: what to run instead."""
        name = command[0]
        if os.sep in name or "/" in name:
            return f"could not start {name}: no such file in {cwd}"
        problem = f"could not start {name}: it is not on PATH"
        found = entry_point(self.root, name)
        args = shlex.join(command[1:])
        for folder in (self.root / ".venv" / "bin", self.root / ".venv" / "Scripts"):
            if (folder / name).exists() or (folder / f"{name}.exe").exists():
                return f"{problem}. The project's environment has it: `{shlex.join([str((folder / name).relative_to(self.root)), *command[1:]])}`"
        if found and "." not in found[1]:
            module, function = found
            code = f"import sys; from {module} import {function}; sys.argv[0] = {name!r}; sys.exit({function}())"
            return f"{problem}. It is the project's script for {module}:{function}; run it with the interpreter: `python -c {shlex.quote(code)} {args}`".rstrip()
        module = name.replace("-", "_").removesuffix(".py")
        return f"{problem}. If it is a Python package's command, run its module with the interpreter: `python -m {module} {args}`".rstrip()

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
                if len(lines) <= 10:
                    lines.append(f"  pid {run['pid']}: {cut(shlex.join(run['argv']), 200)}  ({plural(frames, 'call')}, {exit_text(run)})")
                for e in trace.exceptions(1000)["exceptions"]:
                    if e["handled_in"] is None and e["frame"]:
                        uncaught.append((run["pid"], e))
        if len(processes) > 10:
            lines.append(f"  ... {plural(len(processes) - 10, 'more process')} (`runs` lists them)")
        if not calls:
            lines.append(f"no project code ran (the project root is {self.root})")
        if uncaught:
            lines += ["", "exceptions nothing in the project handled (`traceback` shows each frame's locals):"]
            for pid, e in uncaught[:10]:
                where = f"{e['node']}:{e['line']} (#{e['frame']})"
                lines.append(f"  pid {pid}: {e['type']}: {first_line(e['message'])}  raised in {where}")
            if any(e["type"] not in ("SystemExit", "KeyboardInterrupt") for _, e in uncaught):
                lines.append("next: `traceback` shows the original error, the frames it went through and their locals")
        return lines

    def list_runs(self) -> str:
        runs = sorted(self.runs_dir.glob("*/command.json"), reverse=True) if self.runs_dir.is_dir() else []
        if not runs:
            return "no runs yet: `run` records one" if self.allow_run else f"no runs in {self.runs_dir}"
        lines = []
        for path in runs:
            meta = json.loads(path.read_text())
            processes = processes_of(path.parent)
            status = meta.get("exit_status")
            outcome = "timed out or still running" if status is None else f"exit status {status}"
            lines.append(f"{path.parent.name}  {shlex.join(meta['command'])}  ({outcome}, {plural(len(processes), 'process')} recorded)")
        return "\n".join(lines)

    def tests(self, args: dict) -> str:
        from luciole.outcomes import render_tests, run_tests

        folder = self.run_folder(args.get("run"))
        found = run_tests(processes_of(folder), bool(args.get("failed")))
        if not found["recorded"]:
            raise ToolError(f"run {folder.name} recorded no test outcomes: not a test run, or pytest ran without --luciole")
        text = render_tests(found, int(args.get("limit") or 20), int(args.get("offset") or 0)).replace("(--limit, --offset)", "(limit, offset)")
        failing = next((test for test in found["tests"] if test["exception"] and test["outcome"] in ("failed", "error")), None)
        if failing:
            text += f"\nnext: `traceback` exception={failing['exception']['id']} process={failing['pid']} shows the first failure's original error and each frame's locals"
        return f"run {folder.name}\n{text}"

    def suspects(self, args: dict) -> str:
        from luciole.localize import suspects
        from luciole.localize_text import render_suspects

        folder = self.run_folder(args.get("run"))
        try:
            graph = self.static_graph()  # files and lines
        except (OSError, SyntaxError):
            graph = None
        found = suspects(processes_of(folder), graph, int(args.get("top") or 10))
        return f"run {folder.name}\n" + render_suspects(found, hints="mcp")

    def compare(self, args: dict) -> str:
        from luciole.localize import compare, compare_tests
        from luciole.localize_text import render_compare

        run_b = args.get("run_b")
        folder_a = self.run_folder(args.get("run"))
        folder_b = self.run_folder(run_b) if run_b else folder_a
        try:
            if args.get("frame_a") is not None or args.get("frame_b") is not None:
                if args.get("frame_a") is None or args.get("frame_b") is None:
                    raise ToolError("give both frame_a and frame_b, or neither (the first failing test and the closest passing one)")
                process_b = args.get("process_b", None if run_b else args.get("process"))
                with self.trace(folder_a.name, args.get("process")) as trace_a, self.trace(folder_b.name, process_b) as trace_b:
                    found = compare(trace_a, int(args["frame_a"]), trace_b, int(args["frame_b"]))
                    found["pid_a"], found["pid_b"] = trace_a.run()["pid"], trace_b.run()["pid"]
            else:
                found = compare_tests(processes_of(folder_a), processes_of(folder_b) if run_b else None)
        except LookupError as problem:
            raise ToolError(str(problem).strip("'")) from None
        if run_b:
            found["run_a"], found["run_b"] = (folder_b.name, folder_a.name) if found.get("swapped") else (folder_a.name, folder_b.name)
        return render_compare(found, hints="mcp")

    def calls(self, trace: Trace, args: dict, page: tuple[int, int]) -> str:
        caller = args.get("caller") or None
        found = trace.calls(required(args, "function"), *page, caller=caller)
        if not found["nodes"]:
            raise ToolError(f"no calls of {args['function']} in this run: it did not run, or the name is wrong (`find` looks it up)")
        if caller and not found["total"]:
            sites = ", ".join(f"{site['node'] or 'a root'}:{site['line']} ×{site['count']}" for site in found["callers"][:10])
            raise ToolError(f"no calls of {', '.join(found['nodes'])} from {caller}; it was called from {sites}")
        text = render_calls(found, detail=bool(args.get("detail"))).replace("(--limit, --offset)", "(limit, offset)")
        if not args.get("detail") and cut_short(found["calls"]):
            text += f"\nvalues cut short: `frame` with frame={found['calls'][0]['frame']} (or another #N) shows a call's in full"
        return text

    def call_tree(self, trace: Trace, args: dict) -> str:
        start, depth = args.get("frame"), args.get("depth")
        start = int(start) if start is not None else None
        if args.get("focus"):
            found = trace.focus(args["focus"], start, int(depth) if depth is not None else 1)
            if not found["nodes"]:
                raise ToolError(f"no calls of {args['focus']} in this run: it did not run, or the name is wrong (`find` looks it up)")
            return render_focus(found)
        entries = trace.tree(start, int(depth) if depth is not None else 4)
        if not entries:
            return "no calls recorded" if start is None else f"no frame #{start} in this process"
        folded = fold_small(entries)
        text = render_tree(folded)
        if "small call" in text or "calls below)" in text:
            text += "\nnext: `call_tree` with focus=FUNCTION keeps only the paths to its calls; frame=#N opens one call"
        return text

    def frame(self, trace: Trace, frame_id: int) -> str:
        frame = trace.frame(frame_id)
        if frame is None:
            raise ToolError(f"no frame #{frame_id} in this process")
        text = render_calls({"nodes": [frame["node"]], "total": 1, "calls": [frame]}, detail=True).split("\n", 1)[1].strip()
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
            again = (
                f'To record it, `run` the same command again with scope: ["{node}"] (level 2), then ask `variable_history` again.'
                if self.allow_run
                else f"This server cannot run programs: the user can record it again with level 2 and scope {node} (`pytest --luciole --luciole-level 2 --luciole-scope {node}`)."
            )
            raise ToolError(f"no variable history for {', '.join(found['nodes'])} in this run ({had}; {plural(found['calls'], 'call')} recorded, see `calls`). {again}")
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
        return render_origin(found, rerun="mcp" if self.allow_run else "user")

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

    def run_folder(self, run_id: str | None) -> Path:
        folders = sorted(path.parent for path in self.runs_dir.glob("*/command.json")) if self.runs_dir.is_dir() else []
        if not folders:
            raise ToolError("no runs yet: `run` records one" if self.allow_run else f"no runs in {self.runs_dir}")
        if run_id:
            folder = self.runs_dir / str(run_id)
            if folder not in folders:
                raise ToolError(f"no run {run_id} (`runs` lists them)")
            return folder
        return folders[-1]

    def trace(self, run_id: str | None, pid: int | None) -> Trace:
        folder = self.run_folder(run_id)
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


def clip(text: str, room: int, path: Path) -> str:
    """A program's output in at most about `room` characters: its start and its end, whole lines
    (each cut to LINE_CHARS), and where the rest is."""
    lines = [cut(line, LINE_CHARS) for line in text.rstrip("\n").splitlines()]
    if not lines:
        return "(none)"
    if sum(len(line) + 3 for line in lines) <= room:
        return "\n".join(lines)
    head, size = [], 0
    for line in lines:
        if size + len(line) + 3 > room * OUTPUT_HEAD:
            break
        head.append(line)
        size += len(line) + 3
    tail = []
    for line in reversed(lines[len(head) :]):
        if size + len(line) + 3 > room:
            break
        tail.append(line)
        size += len(line) + 3
    left = lines[len(head) : len(lines) - len(tail)]
    marker = f"... {plural(len(left), 'line')} cut ({sum(map(len, left)) // 1000 or 1} KB): the whole output is in {path}"
    return "\n".join(head + [marker] + tail[::-1])


def cut(text: str, chars: int) -> str:
    return text if len(text) <= chars else text[: chars - 1] + "…"


def entry_point(root: Path, name: str) -> tuple[str, str] | None:
    """`(module, function)` of the project's console script `name`, from pyproject.toml,
    setup.cfg or setup.py (`cfn-lint = "cfnlint.runner:main"`)."""
    pattern = re.compile(rf"""^\s*["']?{re.escape(name)}["']?\s*=\s*["']?([\w.]+)\s*:\s*([\w.]+)""", re.M)
    for file in ("pyproject.toml", "setup.cfg", "setup.py"):
        try:
            found = pattern.search((root / file).read_text(errors="replace"))
        except OSError:
            continue
        if found:
            return found.group(1), found.group(2)
    return None


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


def serve(root: Path, runs_dir: Path | None = None, allow_run: bool = True) -> int:
    Server(root, runs_dir, allow_run).serve()
    return 0
