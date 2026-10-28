import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import type { Graph } from "../src/graph";
import { buildBrain, MAX_STATE_PAIRS, neuronAt } from "../src/webview/brain";

// out/test/brain.test.js -> repo root
const shop: Graph = JSON.parse(readFileSync(resolve(__dirname, "../../../examples/shop.graph.json"), "utf8"));

function synapses(graph: Graph, kind: string): Record<string, string[]> {
  const result: Record<string, string[]> = {};
  for (const s of buildBrain(graph).synapses.filter((s) => s.kind === kind)) {
    result[`${s.source} > ${s.target}`] = s.variables;
  }
  return result;
}

test("one neuron per function, plus module code that does something", () => {
  const brain = buildBrain(shop);
  assert.deepEqual(brain.neurons.map((n) => n.id).sort(), [
    "shop",
    "shop:Cart.__init__",
    "shop:Cart.add",
    "shop:Cart.total",
    "shop:main",
    "shop:with_tax",
  ]);
  const add = brain.neurons.find((n) => n.id === "shop:Cart.add")!;
  assert.deepEqual([add.label, add.module, add.params], ["Cart.add", "shop", ["price"]]);
});

test("call synapses carry arguments and return values", () => {
  assert.deepEqual(synapses(shop, "call"), {
    "shop > shop:main": [],
    "shop:Cart.total > shop:with_tax": ["subtotal → amount", "return ← return"],
    "shop:main > shop:Cart.__init__": [],
    "shop:main > shop:Cart.add": ["cart → self"],
    "shop:main > shop:Cart.total": ["cart → self", "due ← return"],
  });
  const add = buildBrain(shop).synapses.find((s) => s.target === "shop:Cart.add")!;
  assert.equal(add.count, 2); // two call sites
});

test("state synapses go from writers to readers of globals and attributes", () => {
  assert.deepEqual(synapses(shop, "state"), {
    "shop > shop:with_tax": ["TAX_RATE"],
    "shop:Cart.__init__ > shop:Cart.add": ["subtotal"],
    "shop:Cart.__init__ > shop:Cart.total": ["subtotal"],
    "shop:Cart.add > shop:Cart.total": ["subtotal"],
  });
});

test("very shared variables are left out", () => {
  const n = Math.ceil(Math.sqrt(MAX_STATE_PAIRS)) + 1;
  const fn = (i: number) => ({ id: `m:f${i}`, kind: "function" as const, name: `f${i}`, parent: "m", file: "m.py", loc: { line: 1, col: 0, end_line: 1, end_col: 0 } });
  const graph: Graph = {
    format: "luciole-graph",
    format_version: 1,
    root: ".",
    nodes: [
      { id: "m", kind: "module", name: "m", parent: null, file: "m.py", loc: { line: 1, col: 0, end_line: 1, end_col: 0 } },
      { id: "m#CONFIG", kind: "variable", name: "CONFIG", parent: "m", file: "m.py", loc: { line: 1, col: 0, end_line: 1, end_col: 0 }, role: "global" },
      ...Array.from({ length: 2 * n }, (_, i) => fn(i)),
    ],
    edges: Array.from({ length: n }, (_, i) => [
      { kind: "writes" as const, source: `m:f${i}`, target: "m#CONFIG" },
      { kind: "reads" as const, source: "m#CONFIG", target: `m:f${n + i}` },
    ]).flat(),
  };
  assert.deepEqual(synapses(graph, "state"), {});
});

test("a line belongs to the innermost function around it, else to its module", () => {
  const neurons = buildBrain(shop).neurons;
  assert.equal(neuronAt(neurons, "shop.py", 16)?.id, "shop:Cart.add"); // in Cart.add
  assert.equal(neuronAt(neurons, "shop.py", 7)?.id, "shop:with_tax");
  assert.equal(neuronAt(neurons, "shop.py", 2)?.id, "shop"); // module code
  assert.equal(neuronAt(neurons, "other.py", 7), undefined);
});
