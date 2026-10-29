"""Command-line interface.

    luciole graph <path> [--json] [-o FILE]       static graph of a file or project
    luciole run [options] script.py [args...]     run a program and stream live events
    luciole run [options] -m module [args...]
    luciole engine [--port PORT] [--idle SECONDS]  the local process recorders and viewers connect to
    luciole env [--port PORT] [--root DIR] [--off]  shell commands that turn auto-start on or off
    luciole tests FILE [--root DIR] [--json]      test files that import FILE's module
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
    env_cmd.add_argument("--off", action="store_true", help="print the commands that turn it off")
    env_cmd.add_argument("--shell", choices=["sh", "powershell", "cmd"], default="powershell" if os.name == "nt" else "sh")
    engine_cmd = commands.add_parser("engine", help="run the engine that recorders connect to and viewers subscribe to")
    engine_cmd.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    engine_cmd.add_argument("--idle", type=float, default=600, help="exit after this many seconds with nobody connected (0: never)")
    tests_cmd = commands.add_parser("tests", help="list the test files (test_*.py, *_test.py) that import a file's module")
    tests_cmd.add_argument("file", type=Path, help="Python file")
    tests_cmd.add_argument("--root", type=Path, default=Path.cwd(), help="project directory to search (default: the current directory)")
    tests_cmd.add_argument("--json", action="store_true", help="print a JSON list of absolute paths")
    args = parser.parse_args(argv)

    logging.basicConfig(format="luciole: %(message)s", level=logging.WARNING)
    if args.command == "env":
        if not args.off and not args.port and not engine_port():
            print("luciole: no engine is running yet: programs light up once one runs (open the Luciole view in VS Code, or run `luciole engine`)", file=sys.stderr)
        print(env_commands(auto_start_env(args.port, args.root, args.off), args.shell))
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

        return run(args.target, args.as_module, args.root, args.port, args.events)
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


def auto_start_env(port: int | None, root: Path | None, off: bool) -> dict[str, str | None]:
    """Variables that turn auto-start on (or off), from the current PYTHONPATH. None unsets."""
    entries = [entry for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep) if entry and Path(entry).resolve() != AUTO_START_DIR]
    if off:
        return {"PYTHONPATH": os.pathsep.join(entries) or None, "LUCIOLE_LIVE_PORT": None, "LUCIOLE_ROOT": None}
    return {
        "PYTHONPATH": os.pathsep.join([str(AUTO_START_DIR), *entries]),
        "LUCIOLE_LIVE_PORT": str(port) if port else None,
        "LUCIOLE_ROOT": str(root.resolve()) if root else None,
    }


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
