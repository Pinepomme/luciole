import assert from "node:assert/strict";
import { test } from "node:test";
import { Processes, processLabel } from "../src/processes";

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

test("when a process is done or gone, the snapshot shows the ones still running", () => {
  const processes = new Processes<string>();
  for (const [key, pid] of [["server", 20], ["pytest", 21]] as const) {
    processes.add(key);
    processes.identify(key, { pid, argv: ["python", `${key}.py`] });
  }
  assert.equal(processes.snapshot(), undefined, "nothing active yet");
  processes.merge("server", { stacks: [stack("MainThread", "app:serve")], waiting: { "app:accept": 1 } });
  processes.merge("pytest", { stacks: [stack("MainThread", "tests:test_it")], calls: { "tests:test_it": 1 } });
  // pytest finishes; the idle server sends nothing, so the picture must come from the snapshot.
  assert.equal(processes.merge("pytest", { done: true }), undefined);
  processes.remove("pytest");
  const frame = processes.snapshot();
  assert.deepEqual(frame?.stacks, [stack("MainThread", "app:serve")]);
  assert.deepEqual(frame?.waiting, { "app:accept": 1 });
  assert.equal(frame?.calls, undefined, "no new calls");
  processes.remove("server");
  assert.equal(processes.snapshot(), undefined);
});
