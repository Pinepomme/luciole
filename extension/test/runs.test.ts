import assert from "node:assert/strict";
import { test } from "node:test";
import * as path from "node:path";
import { debugConfiguration, fileRunKind, packageModule, pytestRun, ResolvedRun, resolveRun, splitArgs } from "../src/runs";

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

test("Run Live picks how to run a file", () => {
  assert.equal(fileRunKind("/app/tests/test_cart.py", "def test_x(): pass"), "test");
  assert.equal(fileRunKind("/app/cart_test.py", ""), "test");
  assert.equal(fileRunKind("/app/train.py", "def main(): ...\n\nif __name__ == '__main__':\n    main()\n"), "script");
  assert.equal(fileRunKind("/app/serve.py", 'if __name__ == "__main__":\n    run()'), "script");
  assert.equal(fileRunKind("/app/cart.py", "class Cart:\n    pass\n"), "module");
  assert.equal(fileRunKind("/app/cart.py", "    if __name__ == '__main__':  # nested, not the module's own\n"), "module");
});

test("tests run with pytest from the folder their paths are relative to", () => {
  const config = pytestRun("tests for cart.py", ["/work/app/tests/test_cart.py"], folder);
  assert.deepEqual(config, { name: "tests for cart.py", module: "pytest", args: ["tests/test_cart.py"], folder });
  // Whatever folder is active when it runs (or runs again), it runs from that folder.
  const run = resolveRun(config, { workspaceFolder: config.folder! });
  assert.deepEqual(run, { label: "tests for cart.py", target: ["-m", "pytest", "tests/test_cart.py"], cwd: folder, env: {} });
});

const autoStartDir = "/ext/python/luciole/_autostart";

test("a script runs under the Python debugger with the auto-start variables", () => {
  const run = resolveRun({ name: "train", program: "train.py", args: ["--epochs", "1"], env: { SEED: "1" } }, { workspaceFolder: folder }) as ResolvedRun;
  const config = debugConfiguration(run, { autoStartDir, root: folder, python: "/work/app/.venv/bin/python", pythonPath: "/opt/lib" });
  assert.deepEqual(config, {
    type: "debugpy",
    request: "launch",
    name: "Luciole: train",
    program: "/work/app/train.py",
    args: ["--epochs", "1"],
    cwd: folder,
    env: { SEED: "1", PYTHONPATH: [autoStartDir, "/opt/lib"].join(path.delimiter), LUCIOLE_ROOT: folder },
    python: "/work/app/.venv/bin/python",
    console: "integratedTerminal",
    justMyCode: true,
    __luciole: true,
  });
});

test("modules and tests run under the debugger with -m", () => {
  const tests = resolveRun(pytestRun("tests for cart.py", ["/work/app/tests/test_cart.py"], folder), { workspaceFolder: folder }) as ResolvedRun;
  const config = debugConfiguration(tests, { autoStartDir, root: folder });
  assert.equal(config.module, "pytest");
  assert.equal(config.program, undefined);
  assert.deepEqual(config.args, ["tests/test_cart.py"]);
  assert.equal(config.cwd, folder);
  assert.equal(config.python, undefined); // the Python extension's interpreter
  assert.deepEqual(config.env, { PYTHONPATH: autoStartDir, LUCIOLE_ROOT: folder });
});

test("a run's own PYTHONPATH comes after the auto-start folder", () => {
  const run = resolveRun({ name: "m", module: "app.main", args: ["-v"], env: { PYTHONPATH: "src" } }, { workspaceFolder: folder }) as ResolvedRun;
  const config = debugConfiguration(run, { autoStartDir, root: folder, pythonPath: "/ignored" });
  assert.equal(config.env.PYTHONPATH, [autoStartDir, "src"].join(path.delimiter));
  assert.deepEqual([config.name, config.module, config.args], ["Luciole: m", "app.main", ["-v"]]);
});

test("a package's file that imports relatively runs as a module", () => {
  // /work/rich/rich is a package (it has an __init__.py), /work/rich is not.
  const packages = new Set(["/work/rich/rich", "/work/src/app", "/work/src/app/core"]);
  const isPackage = (dir: string) => packages.has(dir);
  assert.deepEqual(packageModule("/work/rich/rich/progress.py", "from . import filesize, get_console\n", isPackage), { module: "rich.progress", root: "/work/rich" });
  assert.deepEqual(packageModule("/work/src/app/core/engine.py", "from ..store import load\n", isPackage), { module: "app.core.engine", root: "/work/src" });
  assert.deepEqual(packageModule("/work/rich/rich/__main__.py", "from .console import Console\n", isPackage), { module: "rich", root: "/work/rich" });
  // Absolute imports run fine as a script; a relative import outside a package fails either way.
  assert.equal(packageModule("/work/rich/rich/progress.py", "import rich\nfrom rich.console import Console\n", isPackage), undefined);
  assert.equal(packageModule("/work/tools/script.py", "from . import x\n", isPackage), undefined);
});
