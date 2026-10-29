import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import type { Graph } from "../src/graph";
import { breakpointAction, breakpointHint, buildBrain, MAX_STATE_PAIRS, neuronAt } from "../src/webview/brain";

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

test("right-click removes every breakpoint marking a function, else adds one where each call starts", () => {
  const neurons = buildBrain(shop).neurons;
  const neuron = (id: string) => neurons.find((n) => n.id === id)!;
  const main = neuron("shop:main"); // lines 22-27, body_line 23
  assert.deepEqual(breakpointAction(neurons, main, []), { add: 23 });
  assert.deepEqual(breakpointAction(neurons, main, [{ file: "other.py", line: 25 }, { file: "shop.py", line: 7 }]), { add: 23 });
  // A gutter breakpoint on another line of main marks it: right-click removes that one.
  const other = breakpointAction(neurons, main, [{ file: "shop.py", line: 25 }])!;
  assert.deepEqual(other, { remove: [25] });
  assert.equal(breakpointHint(main, other), "remove the breakpoint (line 25)");
  const own = breakpointAction(neurons, main, [{ file: "shop.py", line: 23 }])!;
  assert.equal(breakpointHint(main, own), "remove the breakpoint");
  const several = breakpointAction(neurons, main, [{ file: "shop.py", line: 26 }, { file: "shop.py", line: 23 }, { file: "shop.py", line: 26 }])!;
  assert.deepEqual(several, { remove: [23, 26] });
  assert.equal(breakpointHint(main, several), "remove its 2 breakpoints (lines 23, 26)");
  assert.equal(breakpointHint(main, { add: 23 }), "stop on each call");
  // Module code takes no breakpoints from the brain.
  assert.equal(breakpointAction(neurons, neuron("shop"), [{ file: "shop.py", line: 30 }]), undefined);
});

test("imports and calls matched by name become call synapses", () => {
  const graph: Graph = {
    ...shop,
    nodes: [...shop.nodes, { ...shop.nodes.find((n) => n.kind === "module")!, id: "other", name: "other", file: "other.py" }],
    edges: [
      ...shop.edges,
      { kind: "calls", source: "other", target: "shop", sites: [{ line: 1, col: 0 }] },
      { kind: "calls", source: "other", target: "shop:with_tax", sites: [{ line: 2, col: 0 }], by_name: true },
    ],
  };
  const calls = buildBrain(graph).synapses.filter((s) => s.kind === "call" && s.source === "other");
  assert.deepEqual(calls.map((s) => [s.target, Boolean(s.byName)]).sort(), [["shop", false], ["shop:with_tax", true]]);
});
