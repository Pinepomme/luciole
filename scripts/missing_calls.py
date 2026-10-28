"""How well the static graph predicts the calls a program makes.

    luciole run --root PROJECT --events run.jsonl ...   # record a run first
    uv run python scripts/missing_calls.py PROJECT run.jsonl [--list N]

Prints the share of runtime caller>callee links (between functions and modules, what the view
draws) that the static graph lacks, by kind of callee, and how often static calls between two
functions that ran were seen at runtime (certain edges, and `by_name` guesses apart).
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from luciole.analyzer import build_graph

DUNDERS = "dunder"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project", type=Path)
    parser.add_argument("events", type=Path)
    parser.add_argument("--list", type=int, default=0, help="also list this many missing links")
    args = parser.parse_args()

    graph = build_graph(args.project)
    nodes = {n["id"]: n for n in graph["nodes"]}
    calls = {(e["source"], e["target"]): e for e in graph["edges"] if e["kind"] == "calls"}
    ran, links = set(), Counter()
    for line in args.events.read_text().splitlines():
        frame = json.loads(line)
        ran |= set(frame.get("calls", {}))
        for key, count in frame.get("links", {}).items():
            links[tuple(key.split(">"))] += count

    def drawn(node_id):
        return node_id in nodes and nodes[node_id]["kind"] in ("function", "module")

    def kind(callee):
        node = nodes[callee]
        parent = nodes.get(node["parent"] or "", {})
        if node["kind"] == "module":
            return "module (import)"
        if node["name"].startswith("__") and node["name"].endswith("__"):
            return f"{DUNDERS} {node['name']}"
        if any(d.rpartition(".")[2] in ("property", "cached_property") for d in node.get("decorators", [])):
            return "property"
        return {"class": "method", "function": "nested function"}.get(parent.get("kind"), "function")

    runtime = [pair for pair in links if drawn(pair[0]) and drawn(pair[1])]
    missing = [pair for pair in runtime if pair not in calls]
    print(f"runtime links: {len(runtime)}, missing from the static graph: {len(missing)} ({100 * len(missing) // max(1, len(runtime))}%)")
    by_kind = Counter(DUNDERS if kind(callee).startswith(DUNDERS) else kind(callee) for _, callee in missing)
    for name, count in by_kind.most_common():
        print(f"  {count:5}  {name}")
    for guessed, label in ((False, "certain"), (True, "by name")):
        edges = [pair for pair, edge in calls.items() if bool(edge.get("by_name")) == guessed and pair[0] in ran and pair[1] in ran]
        seen = sum(pair in links for pair in edges)
        print(f"{label} static calls between functions that ran: {len(edges)}, seen at runtime: {seen} ({100 * seen // max(1, len(edges))}%)")
    for caller, callee in sorted(missing)[: args.list]:
        print(f"  {kind(callee):20} {caller} > {callee}")


if __name__ == "__main__":
    main()
