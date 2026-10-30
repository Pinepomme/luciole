"""Command-line interface.

    luciole graph <path> [--json] [-o FILE]       static graph of a file or project
    luciole run [options] script.py [args...]     run a program and stream live events
    luciole run [options] -m module [args...]
    luciole engine [--port PORT] [--idle SECONDS]  the local process recorders and viewers connect to
    luciole env [--port PORT] [--root DIR] [--off]  shell commands that turn auto-start on or off
    luciole tests FILE [--root DIR] [--json]      test files that import FILE's module
    luciole run --trace FILE [--level 0|1] ...      also write a trace of the run (SQLite)
    luciole trace FILE summary|calls NODE|tree|exceptions [--json]   read a trace
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
    run_cmd.add_argument("--level", type=int, choices=[0, 1], default=None, help="trace level: 0 call tree, timings and exceptions (default); 1 also arguments and return values")
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
    env_cmd.add_argument("--level", type=int, choices=[0, 1], help="trace level (see luciole run)")
    env_cmd.add_argument("--off", action="store_true", help="print the commands that turn it off")
    env_cmd.add_argument("--shell", choices=["sh", "powershell", "cmd"], default="powershell" if os.name == "nt" else "sh")
    engine_cmd = commands.add_parser("engine", help="run the engine that recorders connect to and viewers subscribe to")
    engine_cmd.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    engine_cmd.add_argument("--idle", type=float, default=600, help="exit after this many seconds with nobody connected (0: never)")
    tests_cmd = commands.add_parser("tests", help="list the test files (test_*.py, *_test.py) that import a file's module")
    tests_cmd.add_argument("file", type=Path, help="Python file")
    tests_cmd.add_argument("--root", type=Path, default=Path.cwd(), help="project directory to search (default: the current directory)")
    tests_cmd.add_argument("--json", action="store_true", help="print a JSON list of absolute paths")
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
    for query in (summary_cmd, calls_cmd, tree_cmd, exceptions_cmd):  # also accepted after the query
        query.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(format="luciole: %(message)s", level=logging.WARNING)
    if args.command == "trace":
        return trace_command(args)
    if args.command == "env":
        if not args.off and not args.port and not engine_port() and not args.trace:
            print("luciole: no engine is running yet: programs light up once one runs (open the Luciole view in VS Code, or run `luciole engine`)", file=sys.stderr)
        print(env_commands(auto_start_env(args.port, args.root, args.off, args.trace, args.level), args.shell))
        return 0
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
        from luciole.recorder import run

        from luciole.recorder import trace_level

        return run(args.target, args.as_module, args.root, args.port, args.events, args.trace, trace_level(args.level))
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


def auto_start_env(port: int | None, root: Path | None, off: bool, trace: Path | None = None, level: int | None = None) -> dict[str, str | None]:
    """Variables that turn auto-start on (or off), from the current PYTHONPATH. None unsets."""
    entries = [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry and Path(entry).resolve() != AUTO_START_DIR]
    if off:
        variables = {"PYTHONPATH": os.pathsep.join(entries) or None, "LUCIOLE_LIVE_PORT": None, "LUCIOLE_ROOT": None}
        if os.environ.get("LUCIOLE_TRACE") or os.environ.get("LUCIOLE_TRACE_LEVEL"):
            variables.update(LUCIOLE_TRACE=None, LUCIOLE_TRACE_LEVEL=None)
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
    return variables


def trace_command(args) -> int:
    from luciole.trace import Trace, TraceError, render_calls, render_exceptions, render_summary, render_tree

    try:
        trace = Trace(args.file)
    except TraceError as error:
        print(f"luciole: {error}", file=sys.stderr)
        return 1
    with trace:
        if args.query == "summary":
            result, text = trace.summary(args.top), render_summary
        elif args.query == "calls":
            result, text = trace.calls(args.node, args.limit, args.offset), render_calls
        elif args.query == "tree":
            result, text = trace.tree(args.start, args.depth), render_tree
        else:
            result, text = trace.exceptions(args.limit, args.offset), render_exceptions
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.json else text(result))
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
