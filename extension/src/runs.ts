/**
 * What a live run starts: the current file, a configured script or module with arguments, or a
 * module typed in on the spot (`pytest -x tests`). Kept free of `vscode` imports for the tests.
 */

import * as path from "path";

/** One entry of the `luciole.runConfigurations` setting. */
export interface RunConfiguration {
  name: string;
  /** Script to run, relative to the workspace folder. */
  program?: string;
  /** Module to run with `python -m`, instead of `program`. */
  module?: string;
  args?: string[];
  /** Working directory, relative to the workspace folder (default: the folder). */
  cwd?: string;
  env?: Record<string, string>;
  /**
   * Not a setting: the folder (absolute) the run's relative paths are from, and its default
   * working directory. Set for runs Luciole makes up (tests of a file) and kept with the last run,
   * so running it again does not depend on which editor or folder is active.
   */
  folder?: string;
}

export interface ResolvedRun {
  label: string;
  /** What follows `luciole run --root <root>`: the script or `-m module`, then the arguments. */
  target: string[];
  cwd: string;
  env: Record<string, string>;
  /** The script's absolute path, when the run is a script. */
  script?: string;
}

export interface RunVariables {
  workspaceFolder: string;
  /** The active Python file, if any. */
  file?: string;
}

/** Resolves a configuration against the workspace, or explains what is wrong with it. */
export function resolveRun(config: RunConfiguration, variables: RunVariables): ResolvedRun | { error: string } {
  if (!config.name) {
    return { error: "a run configuration needs a name" };
  }
  if (Boolean(config.program) === Boolean(config.module)) {
    return { error: `"${config.name}": give either "program" or "module"` };
  }
  const substitute = (text: string) => substituteVariables(text, variables);
  if (config.args?.some((arg) => typeof arg !== "string")) {
    return { error: `"${config.name}": "args" must be a list of strings` };
  }
  const args = (config.args ?? []).map(substitute);
  const cwd = path.resolve(variables.workspaceFolder, substitute(config.cwd ?? "."));
  const env = Object.fromEntries(Object.entries(config.env ?? {}).map(([key, value]) => [key, substitute(String(value))]));
  if (config.module) {
    return { label: config.name, target: ["-m", substitute(config.module), ...args], cwd, env };
  }
  const script = path.resolve(variables.workspaceFolder, substitute(config.program!));
  return { label: config.name, target: [script, ...args], cwd, env, script };
}

/**
 * How ⚡ Run Live runs a file: a test file with pytest, a script (it has an
 * `if __name__ == "__main__":` block) as itself, and any other module, a library file that is
 * not meant to run on its own, through the tests that import it.
 */
export function fileRunKind(file: string, source: string): "test" | "script" | "module" {
  const name = path.basename(file);
  if (/^test_.*\.py$/.test(name) || /_test\.py$/.test(name)) {
    return "test";
  }
  return /^if\s+__name__\s*==\s*(['"])__main__\1\s*:/m.test(source) ? "script" : "module";
}

/**
 * The module to run a file as, `python -m pkg.mod`, when it can only run that way: it imports
 * relatively (`from . import x`), which fails in a file run as a script, and its folder is a
 * package (`isPackage`: the folder has an `__init__.py`). `root` is the folder the package is
 * imported from. Undefined otherwise: the file runs as a script.
 */
export function packageModule(file: string, source: string, isPackage: (dir: string) => boolean): { module: string; root: string } | undefined {
  if (!/^\s*from\s+\.+\s*[\w.]*\s+import\b/m.test(source)) {
    return undefined;
  }
  const parts = [path.basename(file, ".py")];
  let dir = path.dirname(file);
  while (isPackage(dir) && path.dirname(dir) !== dir) {
    parts.unshift(path.basename(dir));
    dir = path.dirname(dir);
  }
  if (parts.length === 1) {
    return undefined; // not in a package: the relative import fails however it runs
  }
  if (parts[parts.length - 1] === "__main__") {
    parts.pop(); // python -m pkg runs pkg/__main__.py
  }
  // pkg/__init__.py stays `pkg.__init__`: `python -m pkg` would run pkg/__main__.py instead.
  return { module: parts.join("."), root: dir };
}

/** pytest on `tests` (absolute paths), run from `folder` with the paths relative to it. */
export function pytestRun(name: string, tests: string[], folder: string): RunConfiguration {
  return { name, module: "pytest", args: tests.map((test) => path.relative(folder, test) || test), folder };
}

/** A launch configuration for VS Code's Python debugger (debug type `debugpy`). */
export interface PythonDebugConfiguration {
  type: "debugpy";
  request: "launch";
  name: string;
  program?: string;
  module?: string;
  args: string[];
  cwd: string;
  env: Record<string, string>;
  /** The interpreter; when missing, the Python extension's own choice. */
  python?: string;
  console: "integratedTerminal";
  justMyCode: true;
  /** Marks the sessions Luciole starts, to follow them. */
  __luciole: true;
}

export interface DebugOptions {
  /** The auto-start folder (`luciole/_autostart`), whose sitecustomize starts the recorder. */
  autoStartDir: string;
  /** The project the recorder reports on (LUCIOLE_ROOT). */
  root: string;
  python?: string;
  /** PYTHONPATH of the environment the debugger starts from, kept after the auto-start folder. */
  pythonPath?: string;
}

/**
 * `run` under VS Code's Python debugger, with the auto-start variables: breakpoints, stepping,
 * variables and the debug console are the debugger's, and the recorder lights up the brain.
 */
export function debugConfiguration(run: ResolvedRun, options: DebugOptions): PythonDebugConfiguration {
  const [first, ...rest] = run.target;
  const what = first === "-m" ? { module: rest[0], args: rest.slice(1) } : { program: first, args: rest };
  const pythonPath = [options.autoStartDir, run.env.PYTHONPATH ?? options.pythonPath].filter(Boolean).join(path.delimiter);
  return {
    type: "debugpy",
    request: "launch",
    name: `Luciole: ${run.label}`,
    ...what,
    cwd: run.cwd,
    env: { ...run.env, PYTHONPATH: pythonPath, LUCIOLE_ROOT: options.root },
    ...(options.python ? { python: options.python } : {}),
    console: "integratedTerminal",
    justMyCode: true,
    __luciole: true,
  };
}

/** `${workspaceFolder}` and `${file}`, as in VS Code's launch.json. */
export function substituteVariables(text: string, variables: RunVariables): string {
  return text.replace(/\$\{(workspaceFolder|file)\}/g, (match, name: keyof RunVariables) => variables[name] ?? match);
}

/**
 * Splits a command line typed by the user into arguments: spaces separate, single and double
 * quotes group, a backslash escapes the next character (outside single quotes).
 */
export function splitArgs(line: string): string[] {
  const args: string[] = [];
  let current = "";
  let started = false;
  let quote: string | undefined;
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (quote) {
      if (char === quote) {
        quote = undefined;
      } else if (char === "\\" && quote === '"' && i + 1 < line.length) {
        current += line[++i];
      } else {
        current += char;
      }
    } else if (char === "'" || char === '"') {
      quote = char;
      started = true;
    } else if (char === "\\" && i + 1 < line.length) {
      current += line[++i];
      started = true;
    } else if (/\s/.test(char)) {
      if (started) {
        args.push(current);
        current = "";
        started = false;
      }
    } else {
      current += char;
      started = true;
    }
  }
  if (started) {
    args.push(current);
  }
  return args;
}
