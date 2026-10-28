"""Graph format checks shared by the tests: JSON Schema plus cross-reference rules."""

import json
import re
from pathlib import Path

import jsonschema

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "schema" / "graph.schema.json").read_text())

# Allowed (source kinds, target kinds) for each edge kind.
EDGE_ENDPOINTS = {
    "calls": ({"module", "function"}, {"function", "class"}),
    "reads": ({"variable"}, {"module", "function"}),
    "writes": ({"module", "function"}, {"variable"}),
    "flows_to": ({"variable"}, {"variable"}),
    "passes_arg": ({"variable"}, {"variable"}),
    "returns": ({"variable"}, {"variable"}),
}


def expected_id(node, parent):
    if parent is None:
        return node["name"]
    if node["kind"] == "variable":
        return f"{parent['id']}#{node['name']}"
    sep = ":" if parent["kind"] == "module" else "."
    return f"{parent['id']}{sep}{node['name']}"


def check_graph(graph):
    """Return a list of rule violations that JSON Schema cannot express."""
    errors = []
    nodes = {}
    for node in graph["nodes"]:
        if node["id"] in nodes:
            errors.append(f"duplicate node id {node['id']}")
        nodes[node["id"]] = node

    for node in graph["nodes"]:
        parent = nodes.get(node["parent"]) if node["parent"] else None
        if node["parent"] and parent is None:
            errors.append(f"{node['id']}: unknown parent {node['parent']}")
            continue
        if parent and parent["kind"] == "variable":
            errors.append(f"{node['id']}: parent is a variable")
        if parent and parent["file"] != node["file"]:
            errors.append(f"{node['id']}: file differs from parent's")
        # Redefinitions in the same scope get a ~N suffix.
        if re.sub(r"~\d+$", "", node["id"]) != expected_id(node, parent):
            errors.append(f"{node['id']}: expected id {expected_id(node, parent)}")

    seen = set()
    for edge in graph["edges"]:
        key = (edge["kind"], edge["source"], edge["target"])
        if key in seen:
            errors.append(f"duplicate edge {key}")
        seen.add(key)
        source, target = nodes.get(edge["source"]), nodes.get(edge["target"])
        if source is None or target is None:
            errors.append(f"{key}: unknown endpoint")
            continue
        source_kinds, target_kinds = EDGE_ENDPOINTS[edge["kind"]]
        if source["kind"] not in source_kinds or target["kind"] not in target_kinds:
            errors.append(f"{key}: {source['kind']} -> {target['kind']} not allowed")
        if edge["kind"] == "passes_arg" and target.get("role") != "param":
            errors.append(f"{key}: target is not a parameter")
        if edge["kind"] == "returns" and source.get("role") != "return":
            errors.append(f"{key}: source is not a return value")
    return errors


def assert_valid(graph):
    jsonschema.validate(graph, SCHEMA)
    assert check_graph(graph) == []
