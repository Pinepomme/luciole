/**
 * The Running view's processes: the programs lit up now (a Run Live, programs started with
 * auto-start on, tests, their children), and the last few that exited. Pure logic (no VS Code).
 */

import type { ProcessInfo } from "./processes";

/** Exited processes kept in the list, newest first. */
export const MAX_EXITED = 5;

export interface RunningProcess {
  /** The engine's number for it. */
  id: number;
  info: ProcessInfo;
  exited: boolean;
  /** Order it lit up, then exited in. */
  order: number;
}

export class ProcessList {
  private readonly entries = new Map<number, RunningProcess>();
  private counter = 0;

  /** `id` ran project code (or is Run Live's): it shows as running. */
  start(id: number, info: ProcessInfo): boolean {
    if (this.entries.get(id)?.exited === false) {
      return false;
    }
    this.entries.set(id, { id, info, exited: false, order: this.counter++ });
    return true;
  }

  /** `id` left the engine; true when the list changed (it was shown). */
  exit(id: number): boolean {
    const entry = this.entries.get(id);
    if (!entry || entry.exited) {
      return false;
    }
    entry.exited = true;
    entry.order = this.counter++;
    const exited = this.list.filter((e) => e.exited);
    exited.slice(MAX_EXITED).forEach((e) => this.entries.delete(e.id));
    return true;
  }

  /** The engine went away: every process it knew is gone. */
  exitAll(): boolean {
    let changed = false;
    for (const entry of [...this.entries.values()]) {
      changed = this.exit(entry.id) || changed;
    }
    return changed;
  }

  clearExited(): boolean {
    const exited = this.list.filter((e) => e.exited);
    exited.forEach((e) => this.entries.delete(e.id));
    return exited.length > 0;
  }

  get(id: number): RunningProcess | undefined {
    return this.entries.get(id);
  }

  /** Running ones in the order they started, then exited ones, newest first. */
  get list(): RunningProcess[] {
    const all = [...this.entries.values()];
    const running = all.filter((e) => !e.exited).sort((a, b) => a.order - b.order);
    const exited = all.filter((e) => e.exited).sort((a, b) => b.order - a.order);
    return [...running, ...exited];
  }
}

/** debugpy's options that are flags (the others take a value). */
const DEBUGPY_FLAGS = new Set(["--wait-for-client", "--log-to-stderr"]);

/**
 * The command the user ran, from the interpreter's command line: without the interpreter and its
 * options, without what wraps the program (`luciole run`, debugpy's launch), and with a script by
 * its file name, so `python -m luciole run --root /w /w/app.py -n 3` reads `app.py -n 3` and
 * `python -m pytest -x` reads `pytest -x`.
 */
export function commandLine(argv: string[]): string {
  const args = argv.slice(programStart(argv));
  if (!args.length) {
    return "python";
  }
  const [first, ...rest] = args[0] === "-m" ? args.slice(1) : args;
  return [first.split(/[\\/]/).pop(), ...rest].join(" ");
}

/** Where the program and its arguments start in `argv`, past the interpreter and any wrapper. */
function programStart(argv: string[]): number {
  let i = 1;
  // The interpreter's own options (-u, -X dev, -W error): up to the script or -m.
  const skipInterpreterOptions = () => {
    while (i < argv.length && argv[i].startsWith("-") && argv[i] !== "-m" && argv[i] !== "-c") {
      i += ["-X", "-W", "-Q"].includes(argv[i]) ? 2 : 1;
    }
  };
  skipInterpreterOptions();
  for (;;) {
    const arg = argv[i];
    if (arg === "-m" && argv[i + 1] === "luciole" && argv[i + 2] === "run") {
      // luciole run [--root R] [--port P] [--events F] [-m] target...
      i += 3;
      while (i < argv.length && ["--root", "--port", "--events"].includes(argv[i])) {
        i += 2;
      }
      continue;
    }
    const isDebugpy = arg === "-m" ? argv[i + 1] === "debugpy" : arg !== undefined && /(^|[\\/])debugpy([\\/]?)$/.test(arg);
    if (isDebugpy) {
      // debugpy --connect ADDR [--configure-NAME VALUE]... [--adapter-access-token T] program...
      i += arg === "-m" ? 2 : 1;
      while (i < argv.length && argv[i].startsWith("--")) {
        i += DEBUGPY_FLAGS.has(argv[i]) ? 1 : 2;
      }
      continue;
    }
    return i;
  }
}

export interface ProcessItem {
  id: number;
  /** The command line, with the script's folder left out. */
  label: string;
  /** Its pid and state. */
  description: string;
  tooltip: string;
  /** What can be done to it: stop it (terminal runs), show its debug session, or nothing (exited). */
  action: "stop" | "debug" | "none";
  exited: boolean;
}

/** The view's items; `debugged` tells processes under VS Code's debugger. */
export function processItems(list: RunningProcess[], debugged: (process: RunningProcess) => boolean): ProcessItem[] {
  return list.map((process) => {
    const { info, exited } = process;
    const label = commandLine(info.argv);
    const underDebugger = !exited && debugged(process);
    const state = exited ? "exited" : underDebugger ? "debugging" : "running";
    return {
      id: process.id,
      label,
      description: `pid ${info.pid} · ${state}`,
      tooltip: `${info.argv.join(" ")}\npid ${info.pid}, ${state}`,
      action: exited ? "none" : underDebugger ? "debug" : "stop",
      exited,
    };
  });
}
