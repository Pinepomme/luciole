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

export function engineFile(env: NodeJS.ProcessEnv = process.env): string {
  return path.join(env.LUCIOLE_HOME || path.join(os.homedir(), ".luciole"), `engine-v${PROTOCOL}.json`);
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
  log(`$ ${python} -m luciole engine`);
  const child = spawn(python, ["-m", "luciole", "engine"], {
    cwd: analyzerDir,
    env: { ...process.env, PYTHONPATH: analyzerDir },
    detached: true,
    stdio: "ignore",
  });
  child.on("error", (error) => log(`could not start the engine: ${error.message}`));
  child.unref();
  for (let waited = 0; waited < 10_000; waited += 100) {
    await new Promise((resolve) => setTimeout(resolve, 100));
    const port = readEnginePort();
    if (port !== undefined && port !== running && (await answers(port))) {
      return port;
    }
  }
  throw new Error(`the Luciole engine did not start (${python} -m luciole engine)`);
}
