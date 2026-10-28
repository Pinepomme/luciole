import assert from "node:assert/strict";
import { test } from "node:test";
import { Handles, Processes, processLabel } from "../src/processes";

const stack = (thread: string, ...ids: string[]) => ({ thread, stack: ids });

test("a process counts once it runs project code", () => {
  const processes = new Processes<string>();
  processes.add("pip");
  processes.identify("pip", { pid: 1, argv: ["python", "-m", "pip", "list"] });
  assert.equal(processes.merge("pip", { t: 0.1, stacks: [], calls: {} }), undefined);
  assert.deepEqual(processes.active, []);
  const frame = processes.merge("pip", { t: 0.2, stacks: [stack("MainThread", "app", "app:main")], calls: { "app:main": 1 } });
  assert.deepEqual(frame?.stacks, [stack("MainThread", "app", "app:main")]);
  assert.deepEqual(processes.active, ["pip"]);
});

test("frames carry the stacks of every running process, labelled by process", () => {
  const processes = new Processes<string>();
  for (const [key, pid] of [["a", 10], ["b", 11]] as const) {
    processes.add(key);
    processes.identify(key, { pid, argv: ["/usr/bin/python3", `${key}.py`] });
  }
  processes.merge("a", { stacks: [stack("MainThread", "a:main")], waiting: { "a:fetch": 1 } });
  const frame = processes.merge("b", { stacks: [stack("MainThread", "b:main")], waiting: { "a:fetch": 2 }, calls: { "b:main": 1 } });
  assert.deepEqual(frame?.stacks, [stack("a.py (10) · MainThread", "a:main"), stack("b.py (11) · MainThread", "b:main")]);
  assert.deepEqual(frame?.waiting, { "a:fetch": 3 });
  assert.deepEqual(frame?.calls, { "b:main": 1 });
  // A process that is done leaves the picture; its "done" is not passed on.
  assert.equal(processes.merge("a", { done: true }), undefined);
  assert.deepEqual(processes.merge("b", { stacks: [stack("MainThread", "b:main")] })?.stacks, [stack("b.py (11) · MainThread", "b:main")]);
  processes.remove("a");
  assert.deepEqual(processes.merge("b", { stacks: [stack("MainThread", "b:main")] })?.stacks, [stack("MainThread", "b:main")]);
});

test("processes are named by module or script", () => {
  assert.equal(processLabel({ pid: 7, argv: ["python", "-m", "pytest", "-x"] }), "pytest (7)");
  assert.equal(processLabel({ pid: 8, argv: ["C:\\Python\\python.exe", "-u", "C:\\app\\train.py"] }), "train.py (8)");
  assert.equal(processLabel(undefined), "python");
});

test("handles keep ids of different processes apart", () => {
  const handles = new Handles<string>();
  const parent = handles.handle("parent", 140);
  const child = handles.handle("child", 140); // a forked child's main thread has its parent's ident
  assert.notEqual(parent, child);
  assert.equal(handles.handle("parent", 140), parent);
  assert.deepEqual(handles.resolve(child), ["child", 140]);
  handles.clear("child");
  assert.equal(handles.resolve(child), undefined);
  assert.deepEqual(handles.resolve(parent), ["parent", 140]);
});
