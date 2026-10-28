import assert from "node:assert/strict";
import { test } from "node:test";
import { resolveRun, splitArgs } from "../src/runs";

const folder = "/work/app";

test("splitArgs splits on spaces and keeps quoted groups", () => {
  assert.deepEqual(splitArgs("pytest -x  tests/unit"), ["pytest", "-x", "tests/unit"]);
  assert.deepEqual(splitArgs(`train.py --name "big run" -k 'a b' ""`), ["train.py", "--name", "big run", "-k", "a b", ""]);
  assert.deepEqual(splitArgs(String.raw`--path a\ b --q "say \"hi\""`), ["--path", "a b", "--q", 'say "hi"']);
  assert.deepEqual(splitArgs("   "), []);
});

test("a script runs by absolute path, with variables substituted", () => {
  const run = resolveRun(
    { name: "train", program: "scripts/train.py", args: ["--out", "${workspaceFolder}/out"], cwd: "scripts", env: { SEED: "1" } },
    { workspaceFolder: folder },
  );
  assert.deepEqual(run, {
    label: "train",
    target: ["/work/app/scripts/train.py", "--out", "/work/app/out"],
    cwd: "/work/app/scripts",
    env: { SEED: "1" },
    script: "/work/app/scripts/train.py",
  });
});

test("a module runs with -m from the workspace folder", () => {
  const run = resolveRun({ name: "tests", module: "pytest", args: ["${file}"] }, { workspaceFolder: folder, file: "/work/app/t.py" });
  assert.deepEqual(run, { label: "tests", target: ["-m", "pytest", "/work/app/t.py"], cwd: folder, env: {} });
});

test("a configuration needs exactly one of program and module", () => {
  assert.match((resolveRun({ name: "x" }, { workspaceFolder: folder }) as { error: string }).error, /either/);
  assert.match((resolveRun({ name: "x", program: "a.py", module: "b" }, { workspaceFolder: folder }) as { error: string }).error, /either/);
});
