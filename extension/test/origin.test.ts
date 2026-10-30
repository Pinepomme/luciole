import assert from "node:assert/strict";
import { test } from "node:test";
import { TraceCall, valueItems } from "../src/calls";
import type { Graph } from "../src/graph";
import { callValues, identifierAt, OriginAnswer, OriginItem, originItems, originPath, OriginStep, runTarget, stepText } from "../src/origin";
import { valuesBody } from "../src/values";
import { buildBrain } from "../src/webview/brain";
import { NO_FILTERS, originShades } from "../src/webview/filters";

function step(fields: Partial<OriginStep> & Pick<OriginStep, "kind">): OriginStep {
  return { variable: null, var: null, frame: null, node: null, line: null, code: null, value: null, note: null, children: [], ...fields };
}

// `luciole trace FILE origin prog:leaf n --frame 4` on test_history.py's PROGRAM, as the serve process answers it.
const ANSWER: OriginAnswer = {
  frame: 4,
  node: "prog:leaf",
  variable: "n",
  var: "prog:leaf#n",
  time_ns: 100,
  level: 1,
  scope: null,
  root: "/project",
  files: { "prog:leaf": "prog.py", "prog:main": "prog.py", "prog:middle": "prog.py" },
  text: "where n in prog:leaf #4 came from (level 1 trace):",
  steps: step({
    kind: "argument",
    variable: "n",
    var: "prog:leaf#n",
    frame: 4,
    node: "prog:leaf",
    value: "0",
    caller: { frame: 3, node: "prog:middle", line: 11, code: "total += leaf(i)" },
    children: [
      step({
        kind: "write",
        variable: "i",
        frame: 3,
        node: "prog:middle",
        line: 10,
        code: "for i in range(n):",
        note: "its only assignment",
        children: [
          step({
            kind: "argument",
            variable: "n",
            frame: 3,
            node: "prog:middle",
            value: "5",
            caller: { frame: 2, node: "prog:main", line: 24, code: "middle(5)" },
            children: [step({ kind: "literal", frame: 2, node: "prog:main", line: 24, code: "middle(5)", note: "a literal" })],
          }),
        ],
      }),
    ],
  }),
};

function flatten(items: OriginItem[], depth = 0): string[] {
  return items.flatMap((item) => [`${"  ".repeat(depth)}${item.label}${item.description ? ` | ${item.description}` : ""}`, ...flatten(item.children, depth + 1)]);
}

test("the Origin view reads like render_origin", () => {
  assert.deepEqual(flatten(originItems(ANSWER)), [
    "n = 0 | argument of leaf #4, passed by middle #3, line 11: total += leaf(i)",
    "  i | middle #3, line 10: for i in range(n):   (its only assignment)",
    "    n = 5 | argument of middle #3, passed by main #2, line 24: middle(5)",
    "      no variable | main #2, line 24: middle(5)   (a literal)",
  ]);
  const [root] = originItems(ANSWER);
  assert.deepEqual(root.at, { node: "prog:leaf", line: null });
  assert.deepEqual(root.children[0].at, { node: "prog:middle", line: 10 });
  assert.equal(root.icon, "symbol-parameter");
});

test("steps of every kind", () => {
  assert.deepEqual(stepText(step({ kind: "returned_by", frame: 9, node: "shop:Cart.total", value: "12.0", line: 21, code: "return x" })), {
    label: "returned by Cart.total #9: 12.0",
    description: "line 21: return x",
  });
  assert.equal(stepText(step({ kind: "attribute_written_by", variable: "Cart.subtotal", frame: 6, node: "shop:Cart.add", line: 14 })).label, "Cart.subtotal set by Cart.add #6");
  assert.equal(stepText(step({ kind: "write", variable: "x", frame: 1, node: "m:f", previous: true })).label, "before that: x");
  assert.equal(stepText(step({ kind: "unknown", variable: "y", frame: 1, node: "m:f", note: "why" })).description, "f #1   (why)");
});

test("several assignments offer to record the function line by line; cut walks offer to go on", () => {
  const answer: OriginAnswer = {
    ...ANSWER,
    steps: step({
      kind: "needs_level_2",
      variable: "total",
      frame: 3,
      node: "prog:middle",
      rerun: "prog:middle",
      candidates: [
        { line: 9, code: "total = 0", ran: false },
        { line: 11, code: "total += leaf(i)", ran: true },
      ],
      children: [step({ kind: "write", variable: "x", frame: 3, node: "prog:middle", line: 5, time_ns: 77, cut: "depth" })],
      more: 2,
      not_followed: ["a", "b"],
    }),
  };
  const [root] = originItems(answer);
  assert.equal(root.description, "middle #3: assigned on lines 9, 11");
  assert.deepEqual(
    root.children.map((item) => [item.kind, item.label]),
    [
      ["detail", "line 9: total = 0"],
      ["detail", "line 11: total += leaf(i)"],
      ["rerun", "Record middle line by line and run again"],
      ["step", "x"],
      ["detail", "… 2 more sources not followed (branch limit)"],
    ],
  );
  assert.equal(root.children[1].description, "a call on it ran");
  assert.deepEqual(root.children[1].at, { node: "prog:middle", line: 11 });
  assert.equal(root.children[2].scope, "prog:middle");
  const goOn = root.children[3].children[0];
  assert.equal(goOn.kind, "continue");
  assert.deepEqual(goOn.question, { frame: 3, variable: "x", at: 77 });
});

test("the path on the brain: each step's function, moves toward the question", () => {
  const path = originPath(ANSWER);
  assert.equal(path.start, "prog:leaf");
  assert.deepEqual(path.nodes, ["prog:leaf", "prog:middle", "prog:main"]);
  assert.deepEqual(path.links, [
    { from: "prog:middle", to: "prog:leaf", depth: 1 },
    { from: "prog:main", to: "prog:middle", depth: 3 },
  ]);
  assert.equal(path.label, "n in leaf #4");
});

test("the path's functions stand out, the others are dimmed, hidden ones stay hidden", () => {
  const loc = { line: 1, col: 0, end_line: 2, end_col: 0 };
  const graph: Graph = {
    format: "luciole-graph",
    format_version: 1,
    root: "/p",
    nodes: [
      { id: "m", kind: "module", name: "m", parent: null, file: "m.py", loc },
      ...["a", "b", "c", "_d"].map((name) => ({ id: `m:${name}`, kind: "function" as const, name, parent: "m", file: "m.py", loc })),
    ],
    edges: [{ kind: "calls", source: "m:a", target: "m:b" }],
  };
  const brain = buildBrain(graph);
  const shaded = originShades(brain, { ...NO_FILTERS, hidePrivate: true }, new Set(["m:a", "m:b"]));
  assert.equal(shaded.get("m:a"), undefined);
  assert.equal(shaded.get("m:b"), undefined);
  assert.equal(shaded.get("m:c"), "dim");
  assert.equal(shaded.get("m:_d"), "hidden");
  assert.equal(originShades(brain, { ...NO_FILTERS, hidePrivate: true }, new Set(["m:_d"])).get("m:_d"), undefined);
});

test("the name under the cursor", () => {
  const line = "    total += self.rate * leaf(item.price) or cls.x.y";
  assert.equal(identifierAt(line, 5), "total");
  assert.equal(identifierAt(line, 9), "total"); // just after it
  assert.equal(identifierAt(line, 15), "self.rate");
  assert.equal(identifierAt(line, 20), "self.rate");
  assert.equal(identifierAt(line, 31), "item");
  assert.equal(identifierAt(line, 37), undefined); // item.price: another object's attribute
  assert.equal(identifierAt(line, 42), undefined); // `or`
  assert.equal(identifierAt(line, 47), "cls.x");
  assert.equal(identifierAt("    return shown", 6), "return");
  assert.equal(identifierAt("x = 1e5", 5), undefined);
  assert.equal(identifierAt("x = 1", 4), undefined);
});

test("a call's values to ask about, and the run's command to run again", () => {
  const call: TraceCall = {
    frame: 4,
    node: "m:f",
    caller: null,
    origin: null,
    thread: "MainThread",
    start_ns: 50,
    duration_ns: 10,
    end: "return",
    exception: null,
    args: { n: { type: "int", repr: "2" } },
    result: { type: "int", repr: "4" },
  };
  assert.deepEqual(
    callValues(call).map((v) => [v.variable, v.value?.repr, v.at]),
    [
      ["n", "2", 50],
      ["return", "4", undefined],
    ],
  );
  assert.deepEqual(runTarget(["python", "/p/app.py", "--x"]), { program: "/p/app.py", args: ["--x"] });
  assert.deepEqual(runTarget(["python3", "-m", "pytest", "-x"]), { module: "pytest", args: ["-x"] });
  assert.deepEqual(runTarget(["pytest", "tests"]), { module: "pytest", args: ["tests"] });
  assert.equal(runTarget([]), undefined);
});

test("values ask where they came from", () => {
  const items = valueItems({ x: { type: "int", repr: "3" } }, "where:1", "/p", (variable) => ({ file: "t.sqlite", root: "/p", frame: 7, node: "m:f", variable }));
  assert.deepEqual(items[0].kind === "value" && items[0].origin, { file: "t.sqlite", root: "/p", frame: 7, node: "m:f", variable: "x" });
  const call: TraceCall = { frame: 7, node: "m:f", caller: null, origin: null, thread: "MainThread", start_ns: 50, duration_ns: 1, end: "return", exception: null, args: { a: null }, result: { type: "int", repr: "1" } };
  const body = valuesBody({ call, history: undefined, scopeHint: "", askOrigin: true });
  assert.match(body, /data-origin="a" data-at="50"/);
  assert.match(body, /data-origin="return"/);
  assert.doesNotMatch(valuesBody({ call, history: undefined, scopeHint: "" }), /data-origin/);
});
