import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import { actionItems } from "../src/actions";
import type { Graph, GraphNode } from "../src/graph";
import { MapTree, buildMap, mapDescription, mapFunctions, runningCounts } from "../src/map";
import { MAX_EXITED, ProcessList, commandLine, processItems } from "../src/running";

// out/test/sidebar.test.js -> repo root
const shop: Graph = JSON.parse(readFileSync(resolve(__dirname, "../../../examples/shop.graph.json"), "utf8"));

/** The tree as indented lines: `label (description)`. */
function outline(tree: MapTree, running: string[] = []): string[] {
  const counts = runningCounts(tree, running);
  const lines: string[] = [];
  const walk = (keys: string[], depth: number) => {
    for (const key of keys) {
      const entry = tree.entries.get(key)!;
      const description = mapDescription(entry, counts.get(key) ?? 0);
      lines.push(`${"  ".repeat(depth)}${entry.kind} ${entry.label}${description ? ` (${description})` : ""}`);
      walk(entry.children, depth + 1);
    }
  };
  walk(tree.roots, 0);
  return lines;
}

test("the map of a file: its module, its classes, its functions in source order", () => {
  assert.deepEqual(outline(buildMap(shop)), [
    "module shop (6 functions)",
    "  class Cart (3 functions)",
    "    function __init__",
    "    function add",
    "    function total",
    "  function <module>",
    "  function with_tax",
    "  function main",
  ]);
});

const loc = (line: number) => ({ line, col: 4, end_line: line + 2, end_col: 0 });

/** A project of two packages, as the analyzer would give it (no calls: every function is a neuron). */
function project(): Graph {
  const nodes: GraphNode[] = [];
  const add = (id: string, kind: GraphNode["kind"], parent: string | null, file: string, line = 1) =>
    nodes.push({ id, kind, name: id.slice(Math.max(id.lastIndexOf(":"), id.lastIndexOf(".")) + 1), parent, file, loc: loc(line) });
  add("app.cli", "module", null, "app/cli.py");
  add("app.cli:main", "function", "app.cli", "app/cli.py", 10);
  add("app.core.engine", "module", null, "app/core/engine.py");
  add("app.core.engine:Engine", "class", "app.core.engine", "app/core/engine.py", 3);
  add("app.core.engine:Engine.run", "function", "app.core.engine:Engine", "app/core/engine.py", 8);
  add("app.core.engine:Engine.step", "function", "app.core.engine:Engine", "app/core/engine.py", 4);
  add("app.core.engine:helper", "function", "app.core.engine", "app/core/engine.py", 20);
  add("app.core.store", "module", null, "app/core/store.py");
  add("app.core.store:load", "function", "app.core.store", "app/core/store.py", 2);
  return { format: "luciole-graph", format_version: 1, root: "/work", nodes, edges: [] };
}

test("the map of a folder: packages, modules, classes, as the brain's regions", () => {
  const tree = buildMap(project());
  assert.deepEqual(outline(tree, ["app.core.engine:Engine.run", "app.core.store:load"]), [
    "package app (5 functions · 2 running)",
    "  package core (4 functions · 2 running)",
    "    module engine (3 functions · 1 running)",
    "      class Engine (2 functions · 1 running)",
    "        function step",
    "        function run (running)",
    "      function helper",
    "    module store (1 function · 1 running)",
    "      function load (running)",
    "  module cli (1 function)",
    "    function main",
  ]);
  // Items point at the brain's regions and neurons, and at the code.
  const run = tree.entries.get("fn:app.core.engine:Engine.run")!;
  assert.equal(run.target, "app.core.engine:Engine.run");
  assert.deepEqual([run.file, run.line, run.col, run.container], ["app/core/engine.py", 8, 4, "app.core.engine.Engine"]);
  assert.equal(tree.entries.get(run.parent!)!.target, "cls:app.core.engine:Engine");
  assert.equal(tree.entries.get("pkg:app/core")!.parent, "pkg:app");
  assert.deepEqual(
    mapFunctions(tree).map((entry) => entry.label),
    ["step", "run", "helper", "load", "main"],
  );
});

test("an empty graph has an empty map", () => {
  const tree = buildMap({ format: "luciole-graph", format_version: 1, root: "/w", nodes: [], edges: [] });
  assert.deepEqual(tree.roots, []);
});

test("command lines read as the user typed them", () => {
  assert.equal(commandLine(["/usr/bin/python3", "-m", "pytest", "-x", "tests"]), "pytest -x tests");
  assert.equal(commandLine(["python", "-u", "-X", "dev", "app.py", "--n", "3"]), "app.py --n 3");
  assert.equal(commandLine(["python", "-m", "luciole", "run", "--root", "/w", "--port", "5000", "-m", "pytest", "-q"]), "pytest -q");
  assert.equal(commandLine(["python", "-m", "luciole", "run", "--root", "/w", "/w/app.py", "a/b"]), "app.py a/b");
  assert.equal(commandLine(["C:\\Python\\python.exe", "C:\\app\\train.py"]), "train.py");
  const debugpy = ["/usr/bin/python3", "/home/u/.vscode/extensions/ms-python.debugpy/bundled/libs/debugpy", "--connect", "127.0.0.1:5678", "--configure-qt", "none", "--adapter-access-token", "abc", "/w/app.py", "--n", "3"];
  assert.equal(commandLine(debugpy), "app.py --n 3");
  assert.equal(commandLine(["python", "-m", "debugpy", "--listen", "5678", "--wait-for-client", "-m", "app"]), "app");
  assert.equal(commandLine(["python"]), "python");
});

test("the Running view: lit-up processes first, then the last ones that exited", () => {
  const list = new ProcessList();
  assert.ok(list.start(1, { pid: 101, argv: ["python", "-m", "pytest", "-x"] }));
  assert.ok(!list.start(1, { pid: 101, argv: ["python", "-m", "pytest", "-x"] }), "already shown");
  list.start(2, { pid: 102, argv: ["python", "/w/scripts/train.py", "--small"] });
  list.start(3, { pid: 103, argv: ["python", "/ext/debugpy", "--connect", "1.2.3.4:5", "/w/app.py"] });
  assert.ok(list.exit(1));
  assert.ok(!list.exit(1));
  assert.ok(!list.exit(42), "never shown");
  const items = processItems(list.list, (process) => process.info.pid === 104);
  assert.deepEqual(
    items.map(({ label, description, action }) => [label, description, action]),
    [
      ["train.py --small", "pid 102 · running", "stop"],
      ["app.py", "pid 103 · running", "stop"], // under debugpy, but in no VS Code debug session: it keeps its Stop
      ["pytest -x", "pid 101 · exited", "none"],
    ],
  );
  assert.equal(processItems(list.list, (process) => process.id === 3)[1].action, "debug");
  assert.ok(list.clearExited());
  assert.deepEqual(list.list.map((p) => p.id), [2, 3]);
  assert.ok(list.exitAll());
  assert.deepEqual(list.list.map((p) => p.exited), [true, true]);
});

test("only the last few exited processes are kept", () => {
  const list = new ProcessList();
  for (let id = 1; id <= MAX_EXITED + 3; id++) {
    list.start(id, { pid: id, argv: ["python", `job${id}.py`] });
    list.exit(id);
  }
  assert.equal(list.list.length, MAX_EXITED);
  assert.equal(list.list[0].id, MAX_EXITED + 3, "newest first");
});

test("the Actions view shows the auto-start switch's state", () => {
  const on = actionItems(true).find((item) => item.command === "luciole.toggleAutoStart")!;
  const off = actionItems(false).find((item) => item.command === "luciole.toggleAutoStart")!;
  assert.equal(on.description, "on");
  assert.equal(off.description, "off");
  assert.deepEqual(
    actionItems(false).map((item) => item.command),
    ["luciole.showFileGraph", "luciole.showGraph", "luciole.runLive", "luciole.runLiveWith", "luciole.toggleAutoStart"],
  );
});
