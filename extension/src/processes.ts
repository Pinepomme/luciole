/**
 * The Python processes connected to the view (a Run Live, programs started with auto-start on,
 * their children). Each sends its own frames; the brain gets one picture of all of them.
 */

import type { LiveFrame, ThreadStack } from "./messages";

/** First message of a recorder: which process it is. */
export interface ProcessInfo {
  pid: number;
  /** The interpreter's command line (`sys.orig_argv`). */
  argv: string[];
}

interface Entry {
  info?: ProcessInfo;
  /** Ran project code: a process that never does (pip, a linter) does not count as running. */
  active: boolean;
  stacks: ThreadStack[];
  waiting: Record<string, number>;
}

export class Processes<K> {
  private readonly entries = new Map<K, Entry>();

  add(key: K): void {
    this.entries.set(key, { active: false, stacks: [], waiting: {} });
  }

  identify(key: K, info: ProcessInfo): void {
    const entry = this.entries.get(key);
    if (entry) {
      entry.info = info;
    }
  }

  info(key: K): ProcessInfo | undefined {
    return this.entries.get(key)?.info;
  }

  /** Counts as running from now on, even before it runs project code. */
  activate(key: K): void {
    const entry = this.entries.get(key);
    if (entry) {
      entry.active = true;
    }
  }

  isActive(key: K): boolean {
    return this.entries.get(key)?.active ?? false;
  }

  remove(key: K): void {
    this.entries.delete(key);
  }

  get ids(): K[] {
    return [...this.entries.keys()];
  }

  get active(): K[] {
    return [...this.entries].filter(([, entry]) => entry.active).map(([key]) => key);
  }

  /**
   * `frame` from `key`, with the stacks and waiting coroutines of every active process: frames
   * replace the whole picture in the brain. Threads are labelled with their process when several
   * run. Undefined while `key` has not run project code yet, and for its {"done": true}.
   */
  merge(key: K, frame: LiveFrame): LiveFrame | undefined {
    const entry = this.entries.get(key);
    if (!entry) {
      return undefined;
    }
    if (frame.done) {
      entry.stacks = [];
      entry.waiting = {};
      return undefined;
    }
    entry.stacks = frame.stacks ?? entry.stacks;
    entry.waiting = frame.waiting ?? entry.waiting;
    entry.active ||= Object.keys(frame.calls ?? {}).length > 0 || entry.stacks.length > 0;
    if (!entry.active) {
      return undefined;
    }
    return { ...frame, ...this.picture() };
  }

  /**
   * The stacks and waiting coroutines of the active processes, with no new calls: what the brain
   * shows once a process is done or gone while others still run (those may send nothing for a
   * while). Undefined when no process is active.
   */
  snapshot(): LiveFrame | undefined {
    return this.active.length ? this.picture() : undefined;
  }

  private picture(): { stacks: ThreadStack[]; waiting: Record<string, number> } {
    const active = [...this.entries.values()].filter((e) => e.active);
    const stacks = active.flatMap((e) =>
      active.length > 1 ? e.stacks.map(({ thread, stack }) => ({ thread: `${processLabel(e.info)} · ${thread}`, stack })) : e.stacks,
    );
    const waiting: Record<string, number> = {};
    for (const e of active) {
      for (const [id, count] of Object.entries(e.waiting)) {
        waiting[id] = (waiting[id] ?? 0) + count;
      }
    }
    return { stacks, waiting };
  }
}

/** A short name for a process: its module or script, and its pid. */
export function processLabel(info: ProcessInfo | undefined): string {
  if (!info) {
    return "python";
  }
  const args = info.argv.slice(1);
  const module = args.indexOf("-m");
  const program = module >= 0 ? args[module + 1] : args.find((arg) => !arg.startsWith("-"));
  const name = program?.split(/[\\/]/).pop() ?? "python";
  return `${name} (${info.pid})`;
}
