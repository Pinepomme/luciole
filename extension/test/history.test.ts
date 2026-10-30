import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "node:test";
import {
  argsText,
  callChildren,
  callItem,
  callPageItems,
  exceptionItems,
  HistoryItem,
  moduleFiles,
  outcomeText,
  processHead,
  processItems,
  RecordedProcess,
  RecordedRun,
  runCommand,
  runItems,
  TraceCall,
  tracebackItems,
  valueItems,
} from "../src/calls";
import type { Graph } from "../src/graph";
import type { LiveFrame } from "../src/messages";
import { escapeHtml, valuesBody } from "../src/values";
import { buildBrain } from "../src/webview/brain";
import { filtersText, isPrivate, isTest, neighborhood, NO_FILTERS, shades } from "../src/webview/filters";
import { CHUNK, defaultSpeed, mergeFrames, speedText, Timeline } from "../src/webview/timeline";

// out/test/history.test.js -> repo root
const shop: Graph = JSON.parse(readFileSync(resolve(__dirname, "../../../examples/shop.graph.json"), "utf8"));

const snap = (repr: string, type = "int") => ({ type, repr });

function call(overrides: Partial<TraceCall> = {}): TraceCall {
  return {
    frame: 5,
    node: "shop:Cart.add",
    caller: { frame: 3, node: "shop:main", line: 24 },
    origin: null,
    thread: "MainThread",
    start_ns: 1000,
    duration_ns: 2500,
    end: "return",
    exception: null,
    args: { self: snap("<shop.Cart object at 0x1>", "Cart"), price: snap("10") },
    result: snap("None", "NoneType"),
    count: 1,
    last: 5,
    calls: 0,
    ...overrides,
  };
}

function process(overrides: Partial<RecordedProcess> = {}): RecordedProcess {
  return {
    file: "/runs/r1/trace-1.sqlite",
    pid: 42,
    argv: ["/usr/bin/python3", "/w/shop.py"],
    started_at: 0,
    duration_ns: 8_000_000,
    ended: true,
    exit_status: 0,
    ended_by: "stop",
    frames: 8,
    exceptions: 0,
    uncaught: 0,
    level: 1,
    scope: null,
    truncated: false,
    first_ns: 7_000_000,
    ...overrides,
  };
}

function run(overrides: Partial<RecordedRun> = {}): RecordedRun {
  return {
    id: "20261030-142501",
    folder: "/runs/r1",
    command: ["python", "/w/shop.py"],
    cwd: "/w",
    level: 1,
    scope: null,
    by: "vscode",
    started_at: new Date(2026, 9, 30, 14, 25, 1).getTime() / 1000,
    exit_status: 0,
    state: "finished",
    processes: [process()],
    ...overrides,
  };
}

const now = new Date(2026, 9, 30, 18, 0, 0);
const line = (item: HistoryItem) => `${item.label} | ${item.description ?? ""}`;

test("runs: newest first, Run Live's and agents', with their command, time and status", () => {
  const items = runItems(
    [
      {
        root: "/w",
        folder: "/runs",
        runs: [
          run(),
          run({ id: "b", folder: "/runs/b", command: ["pytest", "-x", "tests"], by: "agent", exit_status: 1, started_at: run().started_at + 60, processes: [process({ uncaught: 1, exceptions: 2 })] }),
          run({ id: "c", folder: "/runs/c", command: ["python", "-m", "pkg.app"], state: "running", exit_status: undefined, started_at: run().started_at - 86400 }),
          run({ id: "d", folder: "/runs/d", state: "empty", processes: [], started_at: run().started_at - 60 }),
        ],
      },
    ],
    now,
  );
  assert.deepEqual(items.map(line), [
    "pytest -x tests | 14:26:01 · exit 1 · agent",
    "shop.py | 14:25:01 · exit 0",
    "shop.py | 14:24:01 · nothing recorded",
    "pkg.app | Oct 29 14:25:01 · running",
  ]);
  assert.deepEqual(
    items.map((item) => [item.icon, item.error, item.expandable]),
    [
      ["error", true, true],
      ["history", false, true],
      ["history", false, false],
      ["pulse", false, true],
    ],
  );
  assert.match(items[2].tooltip!, /Python 3\.12/);
});

test("run commands read as typed", () => {
  assert.equal(runCommand(["python", "-m", "pytest", "-x"]), "pytest -x");
  assert.equal(runCommand(["/venv/bin/python3.12", "/w/app.py", "--n", "3"]), "app.py --n 3");
  assert.equal(runCommand(["pytest", "tests/test_cart.py"]), "pytest tests/test_cart.py");
  assert.equal(runCommand(["/usr/local/bin/uvicorn", "app:api"]), "uvicorn app:api");
});

test("processes, and a process's exceptions before its calls", () => {
  const r = run({ processes: [process(), process({ file: "/runs/r1/trace-2.sqlite", pid: 43, argv: ["python", "-m", "pytest"], frames: 100, uncaught: 1, exceptions: 3, ended: false })] });
  assert.deepEqual(processItems(r, "/w").map(line), ["shop.py | pid 42 · 8 calls · 8.0 ms", "pytest | pid 43 · 100 calls · running · 1 uncaught"]);
  assert.deepEqual(processHead(process(), "/w"), []);
  const [head] = processHead(process({ exceptions: 3, uncaught: 1 }), "/w");
  assert.deepEqual([head.kind, head.label, head.description, head.error], ["exceptions", "Exceptions", "3 · 1 uncaught", true]);
});

test("a call shows its arguments, what it returned or raised, and opens on the calls it made", () => {
  const plain = callItem(call(), "/t.sqlite", "/w");
  assert.equal(line(plain), "Cart.add | (price=10) → None · 2.5 µs");
  assert.equal(plain.expandable, false);
  assert.match(plain.tooltip!, /called by main at line 24/);
  const raised = callItem(call({ node: "app:parse", end: "raise", exception: { id: 1, type: "ValueError", message: "bad 3" }, args: { x: snap("3") }, calls: 2 }), "/t.sqlite", "/w");
  assert.equal(line(raised), "parse | (x=3) ✕ ValueError: bad 3 · 2.5 µs");
  assert.deepEqual([raised.icon, raised.error, raised.expandable], ["error", true, true]);
  assert.equal(outcomeText(call({ end: "unwind", exception: { id: 1, type: "KeyError" } })), "↑ KeyError");
  assert.equal(outcomeText(call({ end: "running", result: undefined })), "[running]");
  // Level 0: no values, the duration only.
  assert.equal(line(callItem(call({ args: undefined, result: undefined }), "/t", "/w")), "Cart.add | 2.5 µs");
  assert.equal(argsText({ s: snap("x".repeat(200), "str") }).length <= 80, true);
});

test("a collapsed call opens on its calls one by one; a page ends with More calls…", () => {
  const repeated = call({ frame: 5, last: 9, count: 3, calls: 0, duration_ns: 7500 });
  assert.deepEqual(callChildren(repeated), { frame: 3, after: 4, until: 9, collapse: false });
  const item = callItem(repeated, "/t", "/w");
  assert.deepEqual([item.label, item.icon, item.expandable], ["Cart.add ×3", "layers", true]);
  assert.deepEqual(callChildren(call({ calls: 4 })), { frame: 5, after: 0, collapse: true });
  assert.equal(callChildren(call()), undefined);
  // A collapsed root (no caller): the roots, from its first frame.
  assert.deepEqual(callChildren(call({ caller: null, count: 2, last: 6 })), { frame: null, after: 4, until: 6, collapse: false });
  const request = { frame: 3, after: 0, collapse: true };
  const items = callPageItems({ calls: [call(), call({ frame: 6 })], next: 6 }, request, "/t", "/w");
  assert.deepEqual(items.map((i) => i.kind), ["call", "call", "more"]);
  const more = items[2];
  assert.equal(more.kind === "more" && more.request.after, 6);
  assert.equal(new Set(items.map((i) => i.key)).size, 3);
});

test("exceptions, the frames they went through, and those frames' locals", () => {
  const exception = { id: 1, frame: 7, line: 15, type: "ValueError", message: "bad 3", cause: null, chain: null, time_ns: 5, handled_in: 3, node: "prog:fails", handled_node: "prog:main" };
  const items = exceptionItems([exception, { ...exception, id: 2, handled_in: null, handled_node: null }], 5, "/t", "/w");
  assert.deepEqual(items.map(line), ["ValueError: bad 3 | fails:15 · handled in main", "ValueError: bad 3 | fails:15 · not handled", "3 more exceptions | luciole trace FILE exceptions --offset …"]);
  const frames = tracebackItems(
    { exception, frames: [call({ node: "prog:fails", end: "raise", at_line: 15, locals: { x: snap("3") } })], handled_in: call({ node: "prog:main", at_line: 31 }) },
    "/t",
    "/w",
  );
  assert.deepEqual(frames.map(line), ["fails | line 15 · raised here", "main | line 31 · handled here"]);
  assert.deepEqual([frames[0].expandable, frames[1].expandable], [true, false]);
  assert.deepEqual(valueItems({ x: snap("3"), cart: null }, "k", "/w").map(line), ["x | 3", "cart | ?"]);
});

test("files that may hold a node not in the graph", () => {
  assert.deepEqual(moduleFiles("/w", "pkg.mod:f"), ["/w/pkg/mod.py", "/w/pkg/mod/__init__.py", "/w/src/pkg/mod.py", "/w/src/pkg/mod/__init__.py"]);
});

test("the values page: arguments, result, locals, and the history when recorded", () => {
  const html = valuesBody({ call: call({ locals: { total: snap("20") } }), history: { nodes: [], scope: null, recorded: false, total: 0, frames: [] }, scopeHint: "luciole run --scope Cart.add …" });
  assert.match(html, /<h2>Arguments<\/h2>/);
  assert.match(html, /price/);
  assert.match(html, /<h2>Returned<\/h2>/);
  assert.match(html, /Local variables as the exception left them/);
  assert.match(html, /Not recorded: .*luciole run --scope Cart\.add/);
  const recorded = valuesBody({
    call: call(),
    history: { nodes: ["shop:Cart.add"], scope: ["Cart.add"], recorded: true, total: 3, frames: [{ ...call(), writes: [{ line: 16, name: "total", value: snap("<b>"), time_ns: 1500 }] }] },
    scopeHint: "",
  });
  assert.match(recorded, /data-line="16"/);
  assert.match(recorded, /&lt;b&gt;/);
  assert.match(recorded, /\+500 ns/);
  assert.match(recorded, /2 more assignments/);
  assert.equal(escapeHtml(`<a href="x">'&'</a>`), "&lt;a href=&quot;x&quot;&gt;&#39;&amp;&#39;&lt;/a&gt;");
});

// Replay

test("replay speed: slow enough to watch, and its label", () => {
  assert.equal(defaultSpeed(1_000_000), 0.0001); // 1 ms: 10 s at 1/10000×
  assert.equal(defaultSpeed(2e9), 0.1); // 2 s: 20 s
  assert.equal(defaultSpeed(100e9), 10);
  assert.deepEqual([speedText(0.001), speedText(1), speedText(10)], ["1/1000×", "1×", "10×"]);
});

test("the timeline asks for frames just ahead, shows them as they come due, and jumps without replaying", () => {
  const timeline = new Timeline(1000, 1000 + 2e9); // 2 s at 1/10×: 5 ms of the run per 50 ms frame
  assert.equal(timeline.period, 5_000_000);
  timeline.play();
  const first = timeline.request()!;
  assert.deepEqual(first, { seq: 0, from: 1000, to: 1000 + CHUNK * 5_000_000, every: 5_000_000 });
  assert.equal(timeline.request(), undefined); // one request at a time
  assert.deepEqual(timeline.tick(50), []); // nothing received yet: the replay waits
  assert.equal(timeline.position, 1000);
  const frames: LiveFrame[] = Array.from({ length: CHUNK }, (_, i) => ({ t: (1000 + (i + 1) * 5_000_000) / 1e9, calls: { [`f${i}`]: 1 } }));
  timeline.receive(first.seq, frames);
  assert.deepEqual(timeline.tick(50).map((f) => Object.keys(f.calls!)[0]), ["f0"]);
  assert.deepEqual(timeline.tick(100).map((f) => Object.keys(f.calls!)[0]), ["f1", "f2"]);
  assert.equal(timeline.request(), undefined); // still 17 frames ahead
  for (let i = 0; i < 8; i++) {
    timeline.tick(50);
  }
  const next = timeline.request()!;
  assert.equal(next.from, first.to);
  // A jump: what was buffered goes, the frame ending at the new position is asked for, the late answer is dropped.
  timeline.seek(1000 + 1e9);
  timeline.receive(next.seq, frames);
  const jump = timeline.request()!;
  assert.deepEqual([jump.from, jump.seq > next.seq], [1000 + 1e9 - 5_000_000, true]);
  timeline.playing = false;
  timeline.receive(jump.seq, [{ t: (1000 + 1e9) / 1e9, stacks: [{ thread: "MainThread", stack: ["m"] }] }]);
  assert.deepEqual(timeline.tick(50)[0].stacks, [{ thread: "MainThread", stack: ["m"] }]); // shown though paused
  assert.equal(timeline.position, 1000 + 1e9);
});

test("the timeline stops at the end, and plays again from the start", () => {
  const timeline = new Timeline(0, 1_000_000);
  timeline.setSpeed(1);
  timeline.play();
  const request = timeline.request()!;
  assert.equal(request.to, 1_000_000); // the whole run fits in one chunk
  timeline.receive(request.seq, []);
  timeline.tick(1e6);
  assert.deepEqual([timeline.position, timeline.playing], [1_000_000, false]);
  timeline.play();
  assert.deepEqual([timeline.position, timeline.playing], [0, true]);
});

test("frames due together are one: calls added up, the last stacks", () => {
  const merged = mergeFrames([
    { t: 1, calls: { a: 1 }, links: { "a>b": 1 }, stacks: [{ thread: "T", stack: ["a"] }] },
    { t: 2, calls: { a: 2, b: 1 }, spawns: { "a>c": 1 }, stacks: [{ thread: "T", stack: ["b"] }] },
  ]);
  assert.deepEqual(merged, { t: 2, stacks: [{ thread: "T", stack: ["b"] }], calls: { a: 3, b: 1 }, links: { "a>b": 1 }, spawns: { "a>c": 1 } });
  assert.equal(mergeFrames([]), undefined);
});

// Focus and filters

test("focus: the functions a few calls around one, either way", () => {
  const brain = buildBrain(shop);
  assert.deepEqual([...neighborhood(brain, "shop:Cart.total", 1)].sort(), ["shop:Cart.total", "shop:main", "shop:with_tax"]);
  assert.deepEqual([...neighborhood(brain, "shop:with_tax", 2)].sort(), ["shop:Cart.total", "shop:main", "shop:with_tax"]);
  const dimmed = shades(brain, { ...NO_FILTERS, around: "shop:with_tax" });
  assert.deepEqual([...dimmed].sort(), [
    ["shop", "dim"],
    ["shop:Cart.__init__", "dim"],
    ["shop:Cart.add", "dim"],
    ["shop:main", "dim"],
  ]);
  assert.equal(shades(brain, { ...NO_FILTERS, around: "nowhere:f" }).size, 0);
  assert.equal(filtersText({ ...NO_FILTERS, around: "shop:with_tax", hideTests: true }, () => "with_tax"), "around with_tax (1 call) · tests hidden");
});

test("filters: tests and private helpers", () => {
  const neuron = (file: string, label: string, kind = "function") => ({ id: label, node: { id: label, kind, name: label, parent: null, file, loc: { line: 1, col: 0, end_line: 2 } }, label, module: "m", params: [], degree: 0 }) as never;
  assert.equal(isTest(neuron("tests/test_cart.py", "test_total")), true);
  assert.equal(isTest(neuron("pkg/cart_test.py", "check")), true);
  assert.equal(isTest(neuron("conftest.py", "fixture")), true);
  assert.equal(isTest(neuron("pkg/cart.py", "TestCart.setup")), true);
  assert.equal(isTest(neuron("pkg/cart.py", "testify")), false);
  assert.equal(isTest(neuron("pkg/contest.py", "total")), false);
  assert.equal(isPrivate(neuron("a.py", "Cart._fill")), true);
  assert.equal(isPrivate(neuron("a.py", "_helper")), true);
  assert.equal(isPrivate(neuron("a.py", "Cart.__init__")), false);
  assert.equal(isPrivate(neuron("_private.py", "_private", "module")), false);
  const brain = buildBrain(shop);
  assert.deepEqual([...shades(brain, { ...NO_FILTERS, hidePrivate: true })], []);
});
