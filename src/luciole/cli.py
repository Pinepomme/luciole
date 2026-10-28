"""Command-line interface.

    luciole graph <path> [--json] [-o FILE]       static graph of a file or project
    luciole run [options] script.py [args...]     run a program and stream live events
    luciole run [options] -m module [args...]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

from luciole import __version__
from luciole.analyzer import build_graph


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
    run_cmd.add_argument("--port", type=int, default=os.environ.get("LUCIOLE_LIVE_PORT"), help="send events to the Luciole view on this port")
    run_cmd.add_argument("--events", type=Path, help="also write events to this JSON Lines file")
    run_cmd.add_argument("-m", dest="as_module", action="store_true", help="run a module, like python -m")
    run_cmd.add_argument("target", nargs=argparse.REMAINDER, help="script (or module) and its arguments")
    args = parser.parse_args(argv)

    logging.basicConfig(format="luciole: %(message)s", level=logging.WARNING)
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


if __name__ == "__main__":
    sys.exit(main())
