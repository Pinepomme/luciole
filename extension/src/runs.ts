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

/** pytest on `tests` (absolute paths), run from `folder` with the paths relative to it. */
export function pytestRun(name: string, tests: string[], folder: string): RunConfiguration {
  return { name, module: "pytest", args: tests.map((test) => path.relative(folder, test) || test), folder };
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
