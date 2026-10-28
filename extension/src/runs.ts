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
