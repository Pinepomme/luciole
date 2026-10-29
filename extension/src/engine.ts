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
  child.on("error", (error) => log(`could not start the engine: ${error.message}`));
  child.unref();
  for (let waited = 0; waited < 10_000; waited += 100) {
    await new Promise((resolve) => setTimeout(resolve, 100));
    const port = readEnginePort();
    if (port !== undefined && port !== running && (await answers(port))) {
      return port;
    }
  }
  let said = "";
  try {
    said = fs.readFileSync(engineLog(), "utf8").trim().split("\n").slice(-3).join(" / ");
  } catch {
    // no output at all
  }
  throw new Error(`the Luciole engine did not start (${python} -m luciole engine)${said ? `: ${said}` : ""}`);
}
