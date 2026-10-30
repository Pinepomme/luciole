/**
 * Recorded runs, read through the bundled analyzer package: `luciole runs` lists and makes run
 * folders, and one `luciole trace FILE serve` process per trace answers the history views'
 * queries (a call's children, replay frames, values), so opening a call or scrubbing does not
 * start Python each time. The processes stop after a minute unused. No VS Code imports.
 */

import { ChildProcess, execFile, spawn } from "child_process";
import type { RunListing } from "./calls";

export interface PythonOptions {
  /** The interpreter (any Python 3.10+: reading traces needs no `sys.monitoring`). */
  python: () => string;
  /** Folder holding the `luciole` package, put on PYTHONPATH. */
  analyzerDir: () => string | undefined;
  log: (line: string) => void;
}

/** A query server stops after this long without a request. */
const IDLE_MS = 60_000;

interface Server {
  process: ChildProcess;
  pending: Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void }>;
  idle?: NodeJS.Timeout;
}

export class Traces {
  private readonly servers = new Map<string, Server>();
  private nextId = 1;

  constructor(private readonly options: PythonOptions) {}

  /** `luciole <args>`, its standard output. */
  run(args: string[]): Promise<string> {
    const dir = this.options.analyzerDir();
    if (!dir) {
      return Promise.reject(new Error("the analyzer is missing from the extension"));
    }
    return new Promise((resolve, reject) => {
      execFile(this.options.python(), ["-m", "luciole", ...args], { cwd: dir, env: { ...process.env, PYTHONPATH: dir }, maxBuffer: 1 << 28 }, (error, stdout, stderr) => {
        if (error) {
          reject(new Error(stderr.trim().split("\n").slice(-2).join("\n") || error.message));
        } else {
          resolve(stdout);
        }
      });
    });
  }

  /** The runs of `root`, newest first (`luciole runs --json`). */
  async list(root: string): Promise<RunListing> {
    return JSON.parse(await this.run(["runs", "--root", root, "--json"])) as RunListing;
  }

  /** A new run folder for a Run Live of `command` in `cwd` (`luciole runs new`). */
  async newRun(root: string, command: string[], cwd: string): Promise<string> {
    const made = JSON.parse(await this.run(["runs", "new", "--root", root, "--cwd", cwd, "--by", "vscode", "--command", JSON.stringify(command), "--json"])) as { folder: string };
    return made.folder;
  }

  /** The run in `folder` ended, with `exitStatus` when known. */
  async finishRun(folder: string, exitStatus: number | undefined): Promise<void> {
    await this.run(["runs", "finish", folder, ...(exitStatus !== undefined ? ["--exit-status", String(exitStatus)] : [])]);
  }

  /** One query of a trace (see `query` in src/luciole/browse.py). */
  query<T>(file: string, name: string, args: Record<string, unknown> = {}): Promise<T> {
    const server = this.server(file);
    const id = this.nextId++;
    clearTimeout(server.idle);
    return new Promise<T>((resolve, reject) => {
      server.pending.set(id, { resolve: resolve as (value: unknown) => void, reject });
      server.process.stdin!.write(JSON.stringify({ id, query: name, args }) + "\n");
    });
  }

  private server(file: string): Server {
    const existing = this.servers.get(file);
    if (existing) {
      return existing;
    }
    const dir = this.options.analyzerDir() ?? "";
    const child = spawn(this.options.python(), ["-m", "luciole", "trace", file, "serve"], { cwd: dir || undefined, env: { ...process.env, PYTHONPATH: dir }, stdio: ["pipe", "pipe", "pipe"] });
    const server: Server = { process: child, pending: new Map() };
    this.servers.set(file, server);
    let buffer = "";
    child.stdout!.setEncoding("utf8");
    child.stdout!.on("data", (chunk: string) => {
      buffer += chunk;
      for (let newline = buffer.indexOf("\n"); newline >= 0; newline = buffer.indexOf("\n")) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        let reply: { id?: number; result?: unknown; error?: string };
        try {
          reply = JSON.parse(line);
        } catch {
          this.options.log(`trace server: ${line.slice(0, 200)}`);
          continue;
        }
        const waiting = reply.id !== undefined ? server.pending.get(reply.id) : undefined;
        if (waiting) {
          server.pending.delete(reply.id!);
          if (reply.error !== undefined) {
            waiting.reject(new Error(reply.error));
          } else {
            waiting.resolve(reply.result);
          }
        }
      }
      if (!server.pending.size) {
        clearTimeout(server.idle);
        server.idle = setTimeout(() => this.close(file), IDLE_MS);
      }
    });
    child.stderr!.setEncoding("utf8");
    child.stderr!.on("data", (text: string) => this.options.log(`trace server: ${text.trimEnd()}`));
    const failed = (error: Error) => {
      if (this.servers.get(file) === server) {
        this.servers.delete(file);
      }
      for (const waiting of server.pending.values()) {
        waiting.reject(error);
      }
      server.pending.clear();
    };
    child.on("error", failed);
    child.on("exit", (code) => failed(new Error(`the trace reader exited (${code})`)));
    return server;
  }

  private close(file: string): void {
    const server = this.servers.get(file);
    if (server && !server.pending.size) {
      this.servers.delete(file);
      server.process.stdin?.end();
    }
  }

  dispose(): void {
    for (const file of [...this.servers.keys()]) {
      const server = this.servers.get(file)!;
      this.servers.delete(file);
      server.process.kill();
    }
  }
}
