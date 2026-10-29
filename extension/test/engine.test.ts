import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { PROTOCOL, engineFile, parseEngineLine, readEnginePort, viewerRoots, waitForEngine } from "../src/engine";

test("the engine's file is per protocol, under LUCIOLE_HOME or ~/.luciole", () => {
  assert.equal(engineFile({ LUCIOLE_HOME: "/tmp/home" }), path.join("/tmp/home", `engine-v${PROTOCOL}.json`));
  assert.equal(engineFile({}), path.join(os.homedir(), ".luciole", `engine-v${PROTOCOL}.json`));
});

test("the port is read from the engine's file", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "luciole-"));
  const file = path.join(dir, "engine.json");
  assert.equal(readEnginePort(file), undefined);
  fs.writeFileSync(file, JSON.stringify({ port: 51234, pid: 1, protocol: PROTOCOL }));
  assert.equal(readEnginePort(file), 51234);
  fs.writeFileSync(file, "{broken");
  assert.equal(readEnginePort(file), undefined);
});

test("viewer roots are sent with symlinks resolved, as recorders resolve theirs", () => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "luciole-")));
  const project = path.join(dir, "project");
  const link = path.join(dir, "link");
  fs.mkdirSync(project);
  fs.symlinkSync(project, link, "junction"); // "junction": no privileges needed on Windows
  const missing = path.join(dir, "missing");
  assert.deepEqual(viewerRoots([link, project, missing]), [project, missing]);
});

test("a broken line from the engine still yields the message it ends with", () => {
  assert.deepEqual(parseEngineLine('{"from":3,"calls":{"m:f":1}}'), { message: { from: 3, calls: { "m:f": 1 } }, recovered: false });
  // A recorder died mid-line: its exit must not be lost.
  assert.deepEqual(parseEngineLine('{"from":3,"stacks":[{"thread":"Ma{"exit":3}'), { message: { exit: 3 }, recovered: true });
  assert.deepEqual(parseEngineLine('{"from":3,"st{"from":4,"stacks":[{"thread":"MainThread","stack":["m:f"]}]}'), {
    message: { from: 4, stacks: [{ thread: "MainThread", stack: ["m:f"] }] },
    recovered: true,
  });
  assert.equal(parseEngineLine('{"from":3,"stacks":[{"thread":"Ma'), undefined);
  assert.equal(parseEngineLine("garbage"), undefined);
  assert.equal(parseEngineLine("[1, 2]"), undefined);
});

test("after starting an engine, the window connects to whichever engine the file names", async () => {
  const fast = { intervalMs: 1, timeoutMs: 1000, triesAfterExit: 3 };
  // Another window's engine won the race: ours exited, and the file names the other one.
  const reads = [undefined, 7001, 7002, 7002];
  let read = 0;
  const port = await waitForEngine({
    ...fast,
    readPort: () => reads[Math.min(read++, reads.length - 1)],
    answers: async (port) => port === 7002, // 7001: a stale file, nothing answers there
    exited: () => read > 1,
  });
  assert.equal(port, 7002);
});

test("the file is read a few more times after the started engine exits, then the wait gives up", async () => {
  let reads = 0;
  const port = await waitForEngine({
    intervalMs: 1,
    timeoutMs: 1000,
    triesAfterExit: 3,
    readPort: () => (reads++, undefined),
    answers: async () => true,
    exited: () => true,
  });
  assert.equal(port, undefined);
  assert.equal(reads, 3);
});
