/**
 * Finding the Luciole engine (`luciole engine`, see src/luciole/engine.py), or starting one: the
 * local process that recorders connect to and viewers, like this extension, subscribe to. Kept free
 * of `vscode` imports for the tests.
 */

import { spawn } from "child_process";
import * as fs from "fs";
import * as net from "net";
import * as os from "os";
import * as path from "path";

/** The live protocol this extension speaks (PROTOCOL in src/luciole/protocol.py). */
export const PROTOCOL = 1;

function home(env: NodeJS.ProcessEnv): string {
  return env.LUCIOLE_HOME || path.join(os.homedir(), ".luciole");
}

export function engineFile(env: NodeJS.ProcessEnv = process.env): string {
  return path.join(home(env), `engine-v${PROTOCOL}.json`);
}

/** Where a started engine's output goes, to tell why it did not start. */
export function engineLog(env: NodeJS.ProcessEnv = process.env): string {
  return path.join(home(env), "engine.log");
}

/** Port in the engine's file, undefined when there is no (readable) file. */
export function readEnginePort(file: string = engineFile()): number | undefined {
  try {
    const port = (JSON.parse(fs.readFileSync(file, "utf8")) as { port?: unknown }).port;
    return typeof port === "number" ? port : undefined;
  } catch {
    return undefined;
  }
}

/**
 * A viewer's roots as the engine compares them: with symlinks resolved, as recorders resolve
 * theirs (a workspace opened through a symlink would otherwise show none of its programs). A
 * folder that cannot be resolved is sent as it is.
 */
export function viewerRoots(folders: string[]): string[] {
  const roots = folders.map((folder) => {
    try {
      return fs.realpathSync(folder);
    } catch {
      return folder;
    }
  });
  return [...new Set(roots)];
}

/** Starts of objects tried when recovering a message from a broken line, last first. */
const MAX_RECOVERY_TRIES = 50;

/**
 * One line from the engine: a JSON object, else undefined. A line can be broken: a recorder that
 * died mid-line left `{"from":3,"sta{"exit":3}` (its partial frame, then the engine's notice that
 * it exited). Losing that exit would keep the process running in the view forever, so a broken
 * line yields the complete object it ends with, if any: an exit notice first, else the last object
 * that parses to the end of the line.
 */
export function parseEngineLine(line: string): { message: Record<string, unknown>; recovered: boolean } | undefined {
  const parse = (text: string) => {
    try {
      const value: unknown = JSON.parse(text);
      return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : undefined;
    } catch {
      return undefined;
    }
  };
  const whole = parse(line);
  if (whole) {
    return { message: whole, recovered: false };
  }
  const exit = line.lastIndexOf('{"exit":');
  const starts = exit > 0 ? [exit] : [];
  for (let start = line.lastIndexOf('{"'); start > 0 && starts.length < MAX_RECOVERY_TRIES; start = line.lastIndexOf('{"', start - 1)) {
    if (start !== exit) {
      starts.push(start);
    }
  }
  for (const start of starts) {
    const message = parse(line.slice(start));
    if (message) {
      return { message, recovered: true };
    }
  }
  return undefined;
}

function answers(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const socket = net.connect(port, "127.0.0.1");
    socket.setTimeout(1000);
    socket.on("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.on("error", () => resolve(false));
    socket.on("timeout", () => {
      socket.destroy();
      resolve(false);
    });
  });
}

export interface WaitOptions {
  /** Port named by the engine's file now, if any. */
  readPort: () => number | undefined;
  /** True when an engine answers on `port`. */
  answers: (port: number) => Promise<boolean>;
  /** True once the engine this window started has exited. */
  exited: () => boolean;
  intervalMs?: number;
  timeoutMs?: number;
  /** Reads of the file still made once the started engine has exited. */
  triesAfterExit?: number;
}

/**
 * Port of the engine the file names once one answers there, else undefined. Not necessarily the
 * engine this window started: when two windows start one at the same time, the second engine to
 * come up finds the first one's file and exits, and both windows connect to the first. So the
 * file is read again a few times after the started engine exits, before giving up.
 */
export async function waitForEngine(options: WaitOptions): Promise<number | undefined> {
  const { readPort, answers, exited, intervalMs = 100, timeoutMs = 10_000, triesAfterExit = 10 } = options;
  let triesLeft = triesAfterExit;
  for (let waited = 0; waited < timeoutMs; waited += intervalMs) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    const port = readPort();
    if (port !== undefined && (await answers(port))) {
      return port;
    }
    if (exited() && --triesLeft <= 0) {
      return undefined;
    }
  }
  return undefined;
}

/**
 * Port of the running engine; starts one (detached: it outlives this window, and exits by itself
 * once nobody uses it) when none answers.
 */
export async function ensureEngine(python: string, analyzerDir: string, log: (line: string) => void): Promise<number> {
  const running = readEnginePort();
  if (running !== undefined && (await answers(running))) {
    return running;
  }
  log(`$ ${python} -m luciole engine  (from ${analyzerDir}, output in ${engineLog()})`);
  fs.mkdirSync(path.dirname(engineLog()), { recursive: true });
  const output = fs.openSync(engineLog(), "w");
  const child = spawn(python, ["-m", "luciole", "engine"], {
    cwd: analyzerDir,
    env: { ...process.env, PYTHONPATH: analyzerDir },
    detached: true,
    stdio: ["ignore", output, output],
  });
  fs.closeSync(output); // the engine has its own copy
  let exited = false;
  child.on("error", (error) => {
    exited = true;
    log(`could not start the engine: ${error.message}`);
  });
  child.on("exit", () => (exited = true));
  child.unref();
  const port = await waitForEngine({ readPort: () => readEnginePort(), answers, exited: () => exited });
  if (port !== undefined) {
    return port;
  }
  let said = "";
  try {
    said = fs.readFileSync(engineLog(), "utf8").trim().split("\n").slice(-3).join(" / ");
  } catch {
    // no output at all
  }
  throw new Error(`the Luciole engine did not start (${python} -m luciole engine)${said ? `: ${said}` : ""}`);
}
