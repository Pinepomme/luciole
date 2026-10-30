"""What VS Code's history views ask of a trace: the frames that replay a run on the brain, and a
small server that keeps a trace open to answer them, so that opening a call or scrubbing the
timeline does not start Python each time.

    luciole trace FILE frames --from S --to S --every MS [--json]   replay frames (the live format)
    luciole trace FILE serve                                        JSON requests on stdin, answers on stdout

The server also answers origin queries (origins.py) for VS Code's "Where did this value come
from?": the static graph is built once per project root and kept while it runs.

Replay frames are what `luciole run` streams live ("Live events" in docs/graph-format.md), made
from the trace: the brain's live lighting replays them as they are.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from luciole.trace import Trace, TraceError, plural

MAX_FRAMES = 2_000  # frames per request
MAX_DEPTH = 200  # frames walked up a stack
MAX_CALLS = 50  # calls listed by `last_calls`


def replay_frames(trace: Trace, start_ns: int, end_ns: int, every_ns: int, limit: int = MAX_FRAMES) -> list[dict]:
    """Live frames for the run between `start_ns` and `end_ns`, one per `every_ns`: each has `t`
    (seconds since the run started, at the end of its window), `stacks` (each thread's project
    frames then, outermost first), and the `calls`, `links` and `spawns` started in its window.
    Coroutines waiting on an await are not known from a trace (only their total time suspended), so
    frames have no `waiting`."""
    every_ns = max(int(every_ns), 1)
    names = dict(trace.db.execute("SELECT id, name FROM threads").fetchall())
    frames = []
    at = int(start_ns)
    while at < end_ns and len(frames) < limit:
        until = min(at + every_ns, int(end_ns))
        calls: dict[str, int] = {}
        links: dict[str, int] = {}
        spawns: dict[str, int] = {}
        rows = trace.db.execute(
            """
            SELECT f.node, c.node AS caller, o.node AS origin, COUNT(*) AS n FROM frames f
            LEFT JOIN frames c ON c.id = f.caller LEFT JOIN frames o ON o.id = f.origin
            WHERE f.start_ns >= ? AND f.start_ns < ? GROUP BY f.node, c.node, o.node
            """,
            (at, until),
        )
        for node, caller, origin, count in rows:
            calls[node] = calls.get(node, 0) + count
            if origin is not None:
                spawns[f"{origin}>{node}"] = spawns.get(f"{origin}>{node}", 0) + count
            elif caller is not None:
                links[f"{caller}>{node}"] = links.get(f"{caller}>{node}", 0) + count
        frame = {"t": until / 1e9, "stacks": stacks_at(trace, until, names), "calls": calls, "links": links, "spawns": spawns}
        frames.append(frame)
        at = until
    return frames


def stacks_at(trace: Trace, at_ns: int, names: dict[int, str] | None = None) -> list[dict]:
    """Each thread's project frames running at `at_ns`, outermost first, the main thread first.

    Walked down from the thread's last root started by then: at each level, the last call its
    frame made by then, while that one still runs. Frame ids follow start times, so "by then" is
    an id bound, and each step is one lookup in the index of callers, however big the trace."""
    names = names if names is not None else dict(trace.db.execute("SELECT id, name FROM threads").fetchall())
    bound = trace.db.execute("SELECT id FROM frames WHERE start_ns <= ? ORDER BY start_ns DESC, id DESC LIMIT 1", (at_ns,)).fetchone()
    if bound is None:
        return []
    stacks = []
    for thread in sorted(names):
        row = trace.db.execute("SELECT id, node, end_ns FROM frames WHERE caller IS NULL AND thread = ? AND id <= ? ORDER BY id DESC LIMIT 1", (thread, bound[0])).fetchone()
        stack = []
        while row is not None and (row["end_ns"] is None or row["end_ns"] > at_ns) and len(stack) < MAX_DEPTH:
            stack.append(row["node"])
            row = trace.db.execute("SELECT id, node, end_ns FROM frames WHERE caller = ? AND id <= ? ORDER BY id DESC LIMIT 1", (row["id"], bound[0])).fetchone()
        if stack:
            stacks.append({"thread": names[thread], "stack": stack})
    return stacks


def query(trace: Trace, name: str, args: dict, graphs: dict | None = None):
    """One request of `serve` (and of the CLI's `--json` queries): the same answers as Trace's.
    `graphs` keeps the static graphs `origin` and `function_at` build, by project root."""
    if name == "origin":
        return origin_query(trace, args, graphs if graphs is not None else {})
    if name == "function_at":
        return function_at(static_graph(trace, graphs if graphs is not None else {}), str(args["file"]), int(args["line"]))
    if name == "last_calls":
        return last_calls(trace, str(args["node"]), int(args.get("limit", MAX_CALLS)))
    if name == "run":
        return trace.run()
    if name == "summary":
        return trace.summary(int(args.get("top", 10)))
    if name == "children":
        return trace.children(args.get("frame"), int(args.get("after", 0)), args.get("until"), int(args.get("limit", 100)), bool(args.get("collapse", True)))
    if name == "frame":
        return trace.frame(int(args["frame"]))
    if name == "exceptions":
        return trace.exceptions(int(args.get("limit", 50)), int(args.get("offset", 0)))
    if name == "traceback":
        return trace.traceback(args.get("exception"))
    if name == "history":
        return trace.history(args.get("node"), args.get("var"), args.get("frame"), int(args.get("limit", 50)), int(args.get("offset", 0)))
    if name == "frames":
        return replay_frames(trace, int(args["from"]), int(args["to"]), int(args["every"]), int(args.get("limit", MAX_FRAMES)))
    raise ValueError(f"unknown query: {name}")


def static_graph(trace: Trace, graphs: dict):
    """The static graph of the trace's project root (`runs.root`), indexed for origins, built once
    per root: the source must still be there, unchanged for its lines to match."""
    from luciole.analyzer import build_graph
    from luciole.slicing import Graph

    root = trace.run()["root"]
    if root not in graphs:
        if not root or not Path(root).is_dir():
            raise ValueError(f"the recorded project root {root} is gone: origins need its source")
        graphs[root] = Graph(build_graph(Path(root)))
    return graphs[root]


def origin_query(trace: Trace, args: dict, graphs: dict) -> dict:
    """Where a value came from (origins.origin), with what a view needs to show it: the project
    `root`, each step's function's `files` (relative to the root), and the answer as `text`."""
    from luciole.origins import origin, render_origin

    graph = static_graph(trace, graphs)
    frame, at = args.get("frame"), args.get("at")
    found = origin(
        trace, graph, args.get("node"), str(args["variable"]), int(frame) if frame is not None else None, int(at) if at is not None else None,
        int(args.get("depth", 8)), int(args.get("branches", 4)),
    )
    nodes: set[str] = set()

    def collect(step: dict) -> None:
        for node in (step.get("node"), (step.get("caller") or {}).get("node"), (step.get("changed_in_place") or {}).get("node")):
            if node:
                nodes.add(node)
        for child in step["children"]:
            collect(child)

    collect(found["steps"])
    found["root"] = str(graph.root)
    found["files"] = {node: file for node in sorted(nodes) if (file := graph.file_of(node))}
    found["text"] = render_origin(found)
    return found


def function_at(graph, file: str, line: int) -> dict | None:
    """The function whose code holds `line` of `file` (absolute, or relative to the root): the
    innermost one, else the module's own code; None outside the project."""
    path = Path(file)
    try:
        relative = (path.resolve().relative_to(graph.root.resolve()) if path.is_absolute() else path).as_posix()
    except ValueError:
        return None
    best = None
    for node in graph.nodes.values():
        loc = node.get("loc")
        if node["kind"] not in ("function", "module") or node.get("file") != relative or not loc or not loc["line"] <= line <= loc["end_line"]:
            continue
        size = (node["kind"] == "function", -(loc["end_line"] - loc["line"]))
        if best is None or size > best[0]:
            best = (size, node)
    return {"node": best[1]["id"], "kind": best[1]["kind"], "line": best[1]["loc"]["line"]} if best else None


def last_calls(trace: Trace, node: str, limit: int = MAX_CALLS) -> dict:
    """The last calls of `node` (an exact node id), newest first, and how many there were."""
    total = trace.db.execute("SELECT COUNT(*) FROM frames WHERE node = ?", (node,)).fetchone()[0]
    rows = trace.db.execute("SELECT * FROM frames WHERE node = ? ORDER BY start_ns DESC, id DESC LIMIT ?", (node, limit)).fetchall()
    return {"node": node, "total": total, "calls": [trace._frame(row) for row in rows]}


def serve(path, stdin=sys.stdin, stdout=sys.stdout) -> int:
    """Answers `{"id": N, "query": NAME, "args": {...}}` lines with `{"id": N, "result": ...}` or
    `{"id": N, "error": TEXT}`, until stdin closes. The trace is opened again for each request, so
    a run still recording is read as it is now; the static graphs origins need are kept."""
    graphs: dict = {}
    for line in stdin:
        if not line.strip():
            continue
        ident = None
        try:
            request = json.loads(line)
            ident = request.get("id")
            with Trace(path) as trace:
                reply = {"id": ident, "result": query(trace, request["query"], request.get("args") or {}, graphs)}
        except (TraceError, ValueError, KeyError, TypeError) as error:
            reply = {"id": ident, "error": str(error)}
        except Exception as error:  # a bug here must not end the session
            reply = {"id": ident, "error": f"{type(error).__name__}: {error}"}
        stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
        stdout.flush()
    return 0


def render_frames(frames: list[dict]) -> str:
    lines = []
    for frame in frames:
        stacks = "  |  ".join(f"{s['thread']}: {' > '.join(s['stack'])}" for s in frame["stacks"]) or "(nothing running)"
        calls = ", ".join(f"{node} ×{count}" for node, count in sorted(frame["calls"].items()))
        lines.append(f"{frame['t']:.6f} s  {stacks}" + (f"\n    started: {calls}" if calls else ""))
    return "\n".join(lines) or "no frames"


def render_children(page: dict) -> str:
    lines = []
    for call in page["calls"]:
        count = f" ×{call['count']}" if call["count"] > 1 else ""
        below = f"  ({plural(call['calls'], 'call')} below)" if call["calls"] else ""
        lines.append(f"#{call['frame']} {call['node']}{count}  [{call['end']}]{below}")
    if page["next"] is not None:
        lines.append(f"... more: --after {page['next']}")
    return "\n".join(lines) or "no calls"
