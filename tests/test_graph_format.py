"""Check the example graphs against the format."""

import json
from pathlib import Path

import jsonschema
import pytest

from graphcheck import SCHEMA, check_graph

EXAMPLES = sorted((Path(__file__).resolve().parent.parent / "examples").glob("*.graph.json"))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_matches_schema(path):
    jsonschema.validate(json.loads(path.read_text()), SCHEMA)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_example_cross_references(path):
    assert check_graph(json.loads(path.read_text())) == []


def test_check_graph_catches_bad_edge():
    graph = json.loads(EXAMPLES[0].read_text())
    graph["edges"].append({"kind": "passes_arg", "source": "shop:main", "target": "shop:with_tax#total"})
    assert any("not allowed" in e for e in check_graph(graph))
