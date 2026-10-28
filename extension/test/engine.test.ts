import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { test } from "node:test";
import { PROTOCOL, engineFile, readEnginePort } from "../src/engine";

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
