import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import type { Graph, GraphEdge, GraphNode } from "../src/graph";
import { buildBrain } from "../src/webview/brain";
import { bundle, buildRegions, hash, labelAnchor, layoutBrain, length, neuronRadius, packItems, regionMembers, route, seedPoint, Vec3 } from "../src/webview/layout";

// out/test/layout.test.js -> repo root
const shop: Graph = JSON.parse(readFileSync(resolve(__dirname, "../../../examples/shop.graph.json"), "utf8"));

const loc = { line: 1, col: 0, end_line: 1, end_col: 0 };
const nodesOf = (graph: Graph) => new Map(graph.nodes.map((n) => [n.id, n]));

/**
 * A made-up project: `files` maps a module's file to its functions (`f`, or `Class.method`),
 * `calls` lists `module:function > module:function`.
 */
function project(files: Record<string, string[]>, calls: string[]): Graph {
  const nodes: GraphNode[] = [];
  for (const [file, functions] of Object.entries(files)) {
    const module = file.replace(/\.py$/, "").replace(/\//g, ".");
    nodes.push({ id: module, kind: "module", name: module, parent: null, file, loc });
    for (const qualified of functions) {
      const [cls, name] = qualified.includes(".") ? qualified.split(".") : [undefined, qualified];
      if (cls && !nodes.some((n) => n.id === `${module}:${cls}`)) {
        nodes.push({ id: `${module}:${cls}`, kind: "class", name: cls, parent: module, file, loc });
      }
      nodes.push({ id: `${module}:${qualified}`, kind: "function", name, parent: cls ? `${module}:${cls}` : module, file, loc });
    }
  }
  const edges: GraphEdge[] = calls.map((call) => {
    const [source, target] = call.split(" > ");
    return { kind: "calls", source, target, sites: [{ line: 1, col: 0 }] };
  });
  return { format: "luciole-graph", format_version: 1, root: ".", nodes, edges };
}

const app = project(
  {
    "src/app/cli.py": ["main", "parse_args"],
    "src/app/core/engine.py": ["Engine.run", "Engine.step", "Engine.stop", "helper"],
    "src/app/core/store.py": ["Store.get", "Store.put", "load"],
    "tests/test_engine.py": ["test_run", "test_stop"],
  },
  [
    "src.app.cli:main > src.app.cli:parse_args",
    "src.app.cli:main > src.app.core.engine:Engine.run",
    "src.app.core.engine:Engine.run > src.app.core.engine:Engine.step",
    "src.app.core.engine:Engine.step > src.app.core.store:Store.get",
    "src.app.core.engine:Engine.step > src.app.core.engine:helper",
    "src.app.core.store:Store.get > src.app.core.store:load",
    "tests.test_engine:test_run > src.app.core.engine:Engine.run",
    "tests.test_engine:test_stop > src.app.core.engine:Engine.stop",
  ],
);

function layout(graph: Graph) {
  return layoutBrain(buildBrain(graph), nodesOf(graph));
}

const distance = (a: Vec3, b: Vec3) => length({ x: a.x - b.x, y: a.y - b.y, z: a.z - b.z });

test("seeds come from names: the same name, the same point in the unit ball", () => {
  assert.equal(hash("pkg.mod"), hash("pkg.mod"));
  assert.notEqual(hash("pkg.mod"), hash("pkg.mud"));
  assert.deepEqual(seedPoint("pkg.mod:f"), seedPoint("pkg.mod:f"));
  for (const name of ["a", "b", "pkg.mod:Class.method", ""]) {
    assert.ok(length(seedPoint(name)) <= 1);
  }
});

test("regions follow directories, modules and classes", () => {
  const { regions, chains } = buildRegions(buildBrain(app), nodesOf(app));
  assert.deepEqual(chains.get("src.app.core.engine:Engine.step"), ["", "pkg:src/app", "pkg:src/app/core", "mod:src.app.core.engine", "cls:src.app.core.engine:Engine"]);
  assert.deepEqual(chains.get("src.app.core.engine:helper"), ["", "pkg:src/app", "pkg:src/app/core", "mod:src.app.core.engine"]);
  // A directory holding a single directory is merged with it: `src` only wraps `src/app`.
  assert.equal(regions.has("pkg:src"), false);
  assert.equal(regions.get("pkg:src/app")!.label, "src/app");
  // tests/ holds a single module: one region, named after both.
  assert.deepEqual(chains.get("tests.test_engine:test_run"), ["", "mod:tests.test_engine"]);
  assert.equal(regions.get("mod:tests.test_engine")!.label, "tests/test_engine");
  assert.deepEqual(regions.get("")!.regions, ["mod:tests.test_engine", "pkg:src/app"]);
  assert.equal(regions.get("pkg:src/app")!.size, 9);
  assert.equal(regions.get("cls:src.app.core.engine:Engine")!.depth, 4);
});

test("a single file: the module, and its classes inside", () => {
  const { regions, chains } = buildRegions(buildBrain(shop), nodesOf(shop));
  assert.deepEqual([...regions.keys()].sort(), ["", "cls:shop:Cart", "mod:shop"]);
  assert.deepEqual(chains.get("shop:Cart.add"), ["", "mod:shop", "cls:shop:Cart"]);
  assert.deepEqual(chains.get("shop:main"), ["", "mod:shop"]);
  assert.deepEqual(chains.get("shop"), ["", "mod:shop"]); // module code
});

test("each neuron lies inside every region holding it, and sibling regions don't overlap", () => {
  for (const graph of [app, shop]) {
    const map = layout(graph);
    const brain = buildBrain(graph);
    for (const neuron of brain.neurons) {
      const position = map.positions.get(neuron.id)!;
      for (const id of map.chains.get(neuron.id)!) {
        const region = map.regions.get(id)!;
        assert.ok(distance(position, region.center) + neuronRadius(neuron.degree) <= region.radius + 1e-6, `${neuron.id} outside ${id}`);
      }
    }
    for (const region of map.regions.values()) {
      const children = region.regions.map((id) => map.regions.get(id)!);
      for (let i = 0; i < children.length; i++) {
        for (let j = i + 1; j < children.length; j++) {
          assert.ok(distance(children[i].center, children[j].center) >= children[i].radius + children[j].radius, `${children[i].id} overlaps ${children[j].id}`);
        }
      }
    }
  }
});

test("the same project gives the same map, whatever the order of its nodes and edges", () => {
  const once = layout(app);
  const reordered = layout({ ...app, nodes: [...app.nodes].reverse(), edges: [...app.edges].reverse() });
  for (const [id, position] of once.positions) {
    const other = reordered.positions.get(id)!;
    assert.ok(distance(position, other) < 1e-9, id);
  }
});

test("editing one module leaves the inside of the others as it was", () => {
  const before = layout(app);
  const edited = project(
    { "src/app/cli.py": ["main", "parse_args", "usage", "version"], ...pick(app) },
    app.edges.map((e) => `${e.source} > ${e.target}`).concat(["src.app.cli:main > src.app.cli:usage"]),
  );
  const after = layout(edited);
  const inside = (map: ReturnType<typeof layout>, id: string) => {
    const region = map.regions.get(map.chains.get(id)!.at(-1)!)!;
    const p = map.positions.get(id)!;
    return { x: p.x - region.center.x, y: p.y - region.center.y, z: p.z - region.center.z };
  };
  for (const id of ["src.app.core.engine:Engine.step", "src.app.core.store:load", "tests.test_engine:test_stop"]) {
    assert.ok(distance(inside(before, id), inside(after, id)) < 1e-9, id);
  }
});

/** The files of `app` but cli.py, as `project` takes them. */
function pick(graph: Graph): Record<string, string[]> {
  const files: Record<string, string[]> = {};
  for (const node of graph.nodes) {
    if (node.kind === "function" && node.file !== "src/app/cli.py") {
      (files[node.file] ??= []).push(node.id.slice(node.id.indexOf(":") + 1));
    }
  }
  return files;
}

test("calls between regions bend through the regions' centres; inside one, they are straight", () => {
  const map = layout(app);
  const centre = (id: string) => map.regions.get(id)!.center;
  assert.deepEqual(route(map, "src.app.core.engine:Engine.run", "src.app.core.engine:Engine.step"), []);
  assert.deepEqual(route(map, "src.app.core.engine:Engine.step", "src.app.core.store:Store.get"), [
    centre("cls:src.app.core.engine:Engine"),
    centre("mod:src.app.core.engine"),
    centre("mod:src.app.core.store"),
    centre("cls:src.app.core.store:Store"),
  ]);
  // From a module's own function into a class of the same module: down only.
  assert.deepEqual(route(map, "src.app.core.engine:helper", "src.app.core.engine:Engine.run"), [centre("cls:src.app.core.engine:Engine")]);
  assert.deepEqual(route(map, "tests.test_engine:test_run", "src.app.core.engine:Engine.run"), [
    centre("mod:tests.test_engine"),
    centre("pkg:src/app"),
    centre("pkg:src/app/core"),
    centre("mod:src.app.core.engine"),
    centre("cls:src.app.core.engine:Engine"),
  ]);
});

test("bundled curves run from start to end; straightened fully, they are the straight line", () => {
  const start = { x: 0, y: 0, z: 0 };
  const end = { x: 10, y: 0, z: 0 };
  assert.deepEqual(bundle(start, [], end), [start, end]);
  const curve = bundle(start, [{ x: 3, y: 5, z: 0 }, { x: 7, y: 5, z: 0 }], end);
  assert.deepEqual(curve[0], start);
  assert.deepEqual(curve.at(-1), end);
  assert.ok(Math.max(...curve.map((p) => p.y)) > 2); // pulled towards the regions
  for (const p of bundle(start, [{ x: 3, y: 5, z: 0 }], end, 0)) {
    assert.ok(Math.abs(p.y) < 1e-9 && p.x >= -1e-9 && p.x <= 10 + 1e-9);
  }
});

test("packing keeps items apart and pulls linked ones together", () => {
  const items = Array.from({ length: 12 }, (_, i) => ({ key: `n:${i}`, radius: 3, region: false }));
  const positions = packItems(items, [{ a: "n:0", b: "n:1", weight: 5 }]);
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length; j++) {
      assert.ok(distance(positions[i], positions[j]) >= 6 - 1e-6);
    }
  }
  const mean = positions.slice(2).reduce((sum, p) => sum + distance(p, positions[0]), 0) / 10;
  assert.ok(distance(positions[0], positions[1]) < mean);
  // A disc: thinner than wide.
  const flat = packItems(Array.from({ length: 40 }, (_, i) => ({ key: `r:${i}`, radius: 10, region: true })), [], 0.3);
  const extent = (axis: keyof Vec3) => Math.max(...flat.map((p) => p[axis])) - Math.min(...flat.map((p) => p[axis]));
  assert.ok(extent("z") < 0.6 * Math.min(extent("x"), extent("y")));
});

test("a region's name goes just above its neurons, not above its packed sphere", () => {
  const map = layout(app);
  const members = regionMembers(map);
  assert.deepEqual(members.get("mod:src.app.core.store")!.sort(), ["src.app.core.store:Store.get", "src.app.core.store:Store.put", "src.app.core.store:load"]);
  assert.equal(members.get("")!.length, buildBrain(app).neurons.length);
  // Seen from the front (right = x, up = y): across, the middle of the points; up, the highest plus the gap.
  const points = [
    { x: 100, y: -50, z: 4 },
    { x: 140, y: -20, z: -4 },
    { x: 120, y: -80, z: 0 },
  ];
  assert.deepEqual(labelAnchor(points, { x: 1, y: 0, z: 0 }, { x: 0, y: 1, z: 0 }, 5), { x: 120, y: -15, z: 0 });
  // Turned: the camera's up is the world's x.
  const turned = labelAnchor(points, { x: 0, y: -1, z: 0 }, { x: 1, y: 0, z: 0 }, 5);
  assert.ok(Math.abs(turned.x - 145) < 1e-9 && Math.abs(turned.y + 50) < 1e-9 && Math.abs(turned.z) < 1e-9);
});
