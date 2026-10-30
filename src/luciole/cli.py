"""Command-line interface.

    luciole graph <path> [--json] [-o FILE]       static graph of a file or project
    luciole run [options] script.py [args...]     run a program and stream live events
    luciole run [options] -m module [args...]
    luciole engine [--port PORT] [--idle SECONDS]  the local process recorders and viewers connect to
    luciole env [--port PORT] [--root DIR] [--off]  shell commands that turn auto-start on or off
    luciole tests FILE [--root DIR] [--json]      test files that import FILE's module
    luciole run --trace FILE [--level 0|1] ...      also write a trace of the run (SQLite)
    luciole run --trace FILE --level 2 --scope PATTERN[,...] ...   also every assignment in those functions
    luciole trace FILE summary|calls NODE|tree|exceptions|traceback|history NODE [--json]   read a trace
    luciole trace FILE children|frame N|frames|serve ...  what VS Code's history views ask (browse.py)
    luciole runs [--root DIR] [--json]            recorded runs of a project (MCP and Run Live), newest first
    luciole runs new --root DIR --command JSON [--cwd DIR] [--level N] [--scope F,G] [--by vscode]   a new run folder; runs finish FOLDER
    luciole trace FILE origin NODE VARIABLE [--frame N] [--depth D] [--json]   where a value came from
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import sys
from collections import Counter
from pathlib import Path

from luciole import __version__
from luciole.analyzer import build_graph
from luciole.protocol import engine_port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="luciole", description="Explore a Python project as a graph.")
    parser.add_argument("--version", action="version", version=f"luciole {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    graph_cmd = commands.add_parser("graph", help="analyze a file or directory and build its graph")
    graph_cmd.add_argument("path", type=Path, help="Python file or project directory")
    graph_cmd.add_argument("--json", action="store_true", help="print the graph as JSON on stdout")
    graph_cmd.add_argument("-o", "--output", type=Path, help="write the graph JSON to this file")
    run_cmd = commands.add_parser("run", help="run a Python program and stream which functions run (Python 3.12+)")
    run_cmd.add_argument("--root", type=Path, default=Path.cwd(), help="project directory: only code inside it is reported")
    run_cmd.add_argument("--port", type=int, default=os.environ.get("LUCIOLE_LIVE_PORT"), help="engine port (default: the running engine's, if any)")
    run_cmd.add_argument("--events", type=Path, help="also write events to this JSON Lines file")
    # A string, not a Path: Path("traces/") drops the separator that marks a directory to make.
    run_cmd.add_argument("--trace", default=os.environ.get("LUCIOLE_TRACE") or None, help="write a trace of the run to this SQLite file (or a new file in this directory)")
    run_cmd.add_argument("--level", type=int, choices=[0, 1, 2], default=None, help="trace level: 0 call tree, timings and exceptions (default); 1 also arguments and return values; 2 also every assignment in the --scope functions")
    run_cmd.add_argument("--scope", default=os.environ.get("LUCIOLE_TRACE_SCOPE") or None, help="level 2: the functions whose variables to record, as node ids or their ends, comma-separated, * as a wildcard (cart:checkout,Cart.*); implies --level 2")
    run_cmd.add_argument("-m", dest="as_module", action="store_true", help="run a module, like python -m")
    run_cmd.add_argument("target", nargs=argparse.REMAINDER, help="script (or module) and its arguments")
    env_cmd = commands.add_parser(
        "env",
        help="print shell commands that make every Python process started from this shell light up the view",
        description='Turn on: eval "$(luciole env)". Turn off: eval "$(luciole env --off)". '
        "PowerShell: luciole env | Invoke-Expression. Programs report to the running engine, found when they start.",
    )
    env_cmd.add_argument("--port", type=int, help="report to the engine on this port rather than the running engine")
    env_cmd.add_argument("--root", type=Path, help="project directory: only code inside it is reported (default: each process's working directory)")
    env_cmd.add_argument("--trace", type=Path, help="also write a trace of each process to a new file in this directory")
    env_cmd.add_argument("--level", type=int, choices=[0, 1, 2], help="trace level (see luciole run)")
    env_cmd.add_argument("--scope", help="level 2: the functions whose variables to record (see luciole run); implies --level 2")
    env_cmd.add_argument("--off", action="store_true", help="print the commands that turn it off")
    env_cmd.add_argument("--shell", choices=["sh", "powershell", "cmd"], default="powershell" if os.name == "nt" else "sh")
    engine_cmd = commands.add_parser("engine", help="run the engine that recorders connect to and viewers subscribe to")
    engine_cmd.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    engine_cmd.add_argument("--idle", type=float, default=600, help="exit after this many seconds with nobody connected (0: never)")
    tests_cmd = commands.add_parser("tests", help="list the test files (test_*.py, *_test.py) that import a file's module")
    tests_cmd.add_argument("file", type=Path, help="Python file")
    tests_cmd.add_argument("--root", type=Path, default=Path.cwd(), help="project directory to search (default: the current directory)")
    tests_cmd.add_argument("--json", action="store_true", help="print a JSON list of absolute paths")
    mcp_cmd = commands.add_parser("mcp", help="an MCP server (stdio) for agents: run programs under the recorder and query the runs")
    mcp_cmd.add_argument("--root", type=Path, default=Path.cwd(), help="project directory (default: the current directory)")
    runs_cmd = commands.add_parser("runs", help="the recorded runs of a project (luciole mcp, VS Code's Run Live), newest first")
    runs_cmd.add_argument("action", nargs="?", choices=["list", "new", "finish"], default="list", help="list (default); new: make a run folder and print it; finish: mark FOLDER's run ended")
    runs_cmd.add_argument("folder", nargs="?", type=Path, help="finish: the run's folder")
    runs_cmd.add_argument("--root", type=Path, default=Path.cwd(), help="project directory (default: the current directory)")
    runs_cmd.add_argument("--cwd", type=Path, help="new: the folder the command runs in (default: the root)")
    runs_cmd.add_argument("--level", type=int, default=1, help="new: the trace level it records at")
    runs_cmd.add_argument("--scope", help="new: the functions level 2 records the variables of, comma-separated")
    runs_cmd.add_argument("--by", default="vscode", help="new: who started it (vscode, agent)")
    runs_cmd.add_argument("--exit-status", type=int, help="finish: the program's exit status, when known")
    runs_cmd.add_argument("--command", dest="run_command", help='new: the command, as a JSON list (["python", "app.py"])')
    runs_cmd.add_argument("--json", action="store_true", help="print JSON")
    trace_cmd = commands.add_parser("trace", help="read a trace written by luciole run --trace")
    trace_cmd.add_argument("file", type=Path, help="trace file (.sqlite)")
    trace_cmd.add_argument("--json", action="store_true", help="print JSON")
    queries = trace_cmd.add_subparsers(dest="query", required=True)
    summary_cmd = queries.add_parser("summary", help="the run, counts, top functions by calls and by time")
    summary_cmd.add_argument("--top", type=int, default=10)
    calls_cmd = queries.add_parser("calls", help="each call of a function: caller, arguments, result or exception, duration")
    calls_cmd.add_argument("node", help="function's node id (pkg.mod:Class.method), or its end (Class.method)")
    calls_cmd.add_argument("--limit", type=int, default=20)
    calls_cmd.add_argument("--offset", type=int, default=0)
    tree_cmd = queries.add_parser("tree", help="the call tree, repeated calls collapsed")
    tree_cmd.add_argument("--from", dest="start", type=int, help="frame id to start from (default: every root)")
    tree_cmd.add_argument("--depth", type=int, default=4)
    exceptions_cmd = queries.add_parser("exceptions", help="exceptions: where raised, chained from, handled where")
    exceptions_cmd.add_argument("--limit", type=int, default=50)
    exceptions_cmd.add_argument("--offset", type=int, default=0)
    traceback_cmd = queries.add_parser("traceback", help="the frames an exception left, with their locals then (level 1)")
    traceback_cmd.add_argument("exception", type=int, nargs="?", help="exception id (default: the last one nothing handled)")
    history_cmd = queries.add_parser("history", help="each call's variable assignments, line by line (level 2)")
    history_cmd.add_argument("node", nargs="?", help="function's node id, or its end (not needed with --frame)")
    history_cmd.add_argument("--var", help="one variable only")
    history_cmd.add_argument("--frame", type=int, help="one call only: its frame id")
    history_cmd.add_argument("--limit", type=int, default=50, help="writes shown")
    history_cmd.add_argument("--offset", type=int, default=0)
    children_cmd = queries.add_parser("children", help="the calls a frame made (default: the roots), a page at a time, repeated calls collapsed")
    children_cmd.add_argument("--frame", type=int, help="frame id (default: the roots)")
    children_cmd.add_argument("--after", type=int, default=0, help="frame id the previous page ended at")
    children_cmd.add_argument("--until", type=int, help="frame id not to go past")
    children_cmd.add_argument("--limit", type=int, default=100)
    children_cmd.add_argument("--no-collapse", dest="collapse", action="store_false", help="one item per call")
    frame_cmd = queries.add_parser("frame", help="one call: arguments, result or exception, locals")
    frame_cmd.add_argument("frame", type=int)
    frames_cmd = queries.add_parser("frames", help="live frames that replay the run (docs/graph-format.md, Live events)")
    frames_cmd.add_argument("--from", dest="start", type=float, default=0, help="seconds since the run started")
    frames_cmd.add_argument("--to", dest="end", type=float, help="seconds (default: the run's end)")
    frames_cmd.add_argument("--every", type=float, default=50, help="milliseconds of the run per frame")
    frames_cmd.add_argument("--limit", type=int, default=2000)
    queries.add_parser("serve", help="answer JSON queries, one per line on stdin (for VS Code)")
    origin_cmd = queries.add_parser("origin", help="where a variable's value came from: the trace and the static graph, walked backwards")
    origin_cmd.add_argument("node", help="function's node id, or its end (- with --frame)")
    origin_cmd.add_argument("variable", help="a local or parameter, return, self.attribute, a global, or a variable's node id")
    origin_cmd.add_argument("--frame", type=int, help="one call: its frame id (default: the function's last call)")
    origin_cmd.add_argument("--at", dest="time_ns", type=int, help="the value at this moment, in ns since the run started (default: the call's end)")
    origin_cmd.add_argument("--depth", type=int, default=8, help="steps followed back (default 8)")
    origin_cmd.add_argument("--branches", type=int, default=4, help="sources followed per step (default 4)")
    for query in (summary_cmd, calls_cmd, tree_cmd, exceptions_cmd, traceback_cmd, history_cmd, origin_cmd, children_cmd, frame_cmd, frames_cmd):  # also accepted after the query
        query.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(format="luciole: %(message)s", level=logging.WARNING)
    if args.command == "trace":
        if args.query == "history" and args.node is None and args.frame is None:
            parser.error("trace history: give a function, or --frame")
        return trace_command(args)
    if args.command == "runs":
        return runs_command(args, parser)
    if args.command in ("run", "env") and not getattr(args, "off", False):
        level, problem = scoped_level(args.level, args.scope)
        if problem:
            parser.error(f"{args.command}: {problem}")
        args.level = level
    if args.command == "env":
        if not args.off and not args.port and not engine_port() and not args.trace:
            print("luciole: no engine is running yet: programs light up once one runs (open the Luciole view in VS Code, or run `luciole engine`)", file=sys.stderr)
        print(env_commands(auto_start_env(args.port, args.root, args.off, args.trace, args.level, args.scope), args.shell))
        return 0
    if args.command == "mcp":
        from luciole.mcp import serve

        return serve(args.root)
    if args.command == "engine":
        from luciole.engine import serve

        return serve(args.port, args.idle)
    if args.command == "tests":
        if not args.file.is_file():
            parser.error(f"no such file: {args.file}")
        from luciole.testfinder import tests_for

        found = tests_for(args.file, args.root)
        print(json.dumps([str(path.resolve()) for path in found]) if args.json else "\n".join(map(str, found)))
        return 0
    if args.command == "run":
        if not args.target:
            parser.error("run: give a script or -m module to run")
        if not args.as_module and not Path(args.target[0]).exists():
            parser.error(f"run: no such file: {args.target[0]}")  # before a trace file is made for it
        from luciole.session import run, trace_level, trace_scope

        return run(args.target, args.as_module, args.root, args.port, args.events, args.trace, trace_level(args.level), trace_scope(args.scope or ""))
    if not args.path.exists():
        parser.error(f"no such file or directory: {args.path}")
    graph = build_graph(args.path)
    text = json.dumps(graph, indent=2)
    if args.output:
        args.output.write_text(text + "\n")
    if args.json:
        print(text)
    else:
        nodes = Counter(n["kind"] for n in graph["nodes"])
        edges = Counter(e["kind"] for e in graph["edges"])
        print("nodes: " + ", ".join(f"{count} {kind}" for kind, count in nodes.items()), file=sys.stderr)
        print("edges: " + ", ".join(f"{count} {kind}" for kind, count in edges.items()), file=sys.stderr)
    return 0


AUTO_START_DIR = Path(__file__).resolve().parent / "_autostart"


def scoped_level(level: int | None, scope: str | list[str] | None) -> tuple[int | None, str | None]:
    """The trace level for `level` and `scope`, or what is wrong with them: a scope means level 2
    (the default with one), and level 2 needs one (it records the variables of chosen functions
    only, being too slow everywhere)."""
    from luciole.session import scope_patterns

    patterns = scope_patterns(scope)
    if patterns and level is not None and level < 2:
        return level, f"a scope records variables, which is level 2, not {level}"
    if level is not None and level >= 2 and not patterns:
        return level, "level 2 records the variables of chosen functions only: give them as a scope (node ids or their ends, * as a wildcard: cart:checkout,Cart.*)"
    return (2 if patterns else level), None


def auto_start_env(port: int | None, root: Path | None, off: bool, trace: Path | None = None, level: int | None = None, scope: str | list[str] | None = None) -> dict[str, str | None]:
    """Variables that turn auto-start on (or off), from the current PYTHONPATH. None unsets."""
    entries = [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry and Path(entry).resolve() != AUTO_START_DIR]
    if off:
        variables = {"PYTHONPATH": os.pathsep.join(entries) or None, "LUCIOLE_LIVE_PORT": None, "LUCIOLE_ROOT": None}
        if os.environ.get("LUCIOLE_TRACE") or os.environ.get("LUCIOLE_TRACE_LEVEL") or os.environ.get("LUCIOLE_TRACE_SCOPE"):
            variables.update(LUCIOLE_TRACE=None, LUCIOLE_TRACE_LEVEL=None, LUCIOLE_TRACE_SCOPE=None)
        return variables
    variables = {
        "PYTHONPATH": os.pathsep.join([str(AUTO_START_DIR), *entries]),
        "LUCIOLE_LIVE_PORT": str(port) if port else None,
        "LUCIOLE_ROOT": str(root.resolve()) if root else None,
    }
    if trace:
        # A directory: each process writes a file of its own there.
        variables["LUCIOLE_TRACE"] = str(trace.resolve()) + os.sep
        variables["LUCIOLE_TRACE_LEVEL"] = str(level) if level is not None else None
        from luciole.session import scope_patterns

        variables["LUCIOLE_TRACE_SCOPE"] = ",".join(scope_patterns(scope)) or None
    return variables


def runs_command(args, parser) -> int:
    from luciole.runs import finish_run, list_runs, new_run, render_runs
    from luciole.session import scope_patterns

    if args.action == "finish":
        if not args.folder or not (args.folder / "command.json").is_file():
            parser.error("runs finish: give a run folder")
        finish_run(args.folder, args.exit_status)
        return 0
    root = args.root.resolve()
    if args.action == "new":
        try:
            command = json.loads(args.run_command or "[]")
        except ValueError:
            command = None
        if not command or not isinstance(command, list):
            parser.error('runs new: give the command as a JSON list: --command \'["python", "app.py"]\'')
        run_id, folder = new_run(root, command, (args.cwd or root).resolve(), args.level, scope_patterns(args.scope) or None, by=args.by)
        print(json.dumps({"id": run_id, "folder": str(folder)}) if args.json else folder)
        return 0
    listing = list_runs(root)
    print(json.dumps(listing, indent=2, ensure_ascii=False) if args.json else render_runs(listing))
    return 0


def trace_command(args) -> int:
    from luciole.browse import query, render_children, render_frames, serve
    from luciole.trace import Trace, TraceError, render_calls, render_exceptions, render_history, render_summary, render_traceback, render_tree

    if args.query == "serve":
        return serve(args.file)
    try:
        trace = Trace(args.file)
    except TraceError as error:
        print(f"luciole: {error}", file=sys.stderr)
        return 1
    with trace:
        if args.query == "origin":
            return origin_command(trace, args)
        if args.query == "summary":
            result, text = trace.summary(args.top), render_summary
        elif args.query == "calls":
            result, text = trace.calls(args.node, args.limit, args.offset), render_calls
        elif args.query == "tree":
            result, text = trace.tree(args.start, args.depth), render_tree
        elif args.query == "traceback":
            result, text = trace.traceback(args.exception), render_traceback
        elif args.query == "history":
            result, text = trace.history(args.node, args.var, args.frame, args.limit, args.offset), render_history
        elif args.query == "children":
            result, text = trace.children(args.frame, args.after, args.until, args.limit, args.collapse), render_children
        elif args.query == "frame":
            result = trace.frame(args.frame)
            text = lambda frame: render_calls({"nodes": [frame["node"]], "total": 1, "calls": [frame]}) if frame else f"no frame #{args.frame}"  # noqa: E731
        elif args.query == "frames":
            end = trace.run()["duration_ns"] if args.end is None else int(args.end * 1e9)
            result = query(trace, "frames", {"from": int(args.start * 1e9), "to": end, "every": int(args.every * 1e6), "limit": args.limit})
            text = render_frames
        else:
            result, text = trace.exceptions(args.limit, args.offset), render_exceptions
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else text(result))
    return 0


def origin_command(trace, args) -> int:
    """`luciole trace FILE origin`: the static graph comes from the trace's own root."""
    from luciole.origins import OriginError, origin, render_origin

    root = Path(trace.run()["root"])
    if not root.is_dir():
        print(f"luciole: the recorded project root {root} is gone: origin needs its source", file=sys.stderr)
        return 1
    try:
        found = origin(trace, build_graph(root), None if args.node == "-" else args.node, args.variable, args.frame, args.time_ns, args.depth, args.branches)
    except OriginError as error:
        print(f"luciole: {error}", file=sys.stderr)
        return 1
    print(json.dumps(found, indent=2, ensure_ascii=False) if args.json else render_origin(found))
    return 0


def env_commands(variables: dict[str, str | None], shell: str) -> str:
    lines = []
    for name, value in variables.items():
        if shell == "powershell":
            quoted = "'" + (value or "").replace("'", "''") + "'"
            lines.append(f"$env:{name} = {quoted}" if value is not None else f"Remove-Item Env:{name} -ErrorAction SilentlyContinue")
        elif shell == "cmd":
            lines.append(f"set {name}={value or ''}")
        else:
            lines.append(f"export {name}={shlex.quote(value)}" if value is not None else f"unset {name}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
