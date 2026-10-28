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
    return { ...frame, stacks, waiting };
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

/**
 * Numbers that are unique per process (thread idents, value refs) made unique across processes:
 * a forked child's main thread has its parent's ident, and every recorder counts refs from 1.
 */
export class Handles<K> {
  private next = 1;
  private readonly byHandle = new Map<number, [K, number]>();
  private readonly bySource = new Map<K, Map<number, number>>();

  handle(key: K, id: number): number {
    let ids = this.bySource.get(key);
    if (!ids) {
      ids = new Map();
      this.bySource.set(key, ids);
    }
    let handle = ids.get(id);
    if (handle === undefined) {
      handle = this.next++;
      ids.set(id, handle);
      this.byHandle.set(handle, [key, id]);
    }
    return handle;
  }

  resolve(handle: number): [K, number] | undefined {
    return this.byHandle.get(handle);
  }

  /** Forget the handles of `key`, or all of them. */
  clear(key?: K): void {
    for (const [handle, [owner]] of this.byHandle) {
      if (key === undefined || owner === key) {
        this.byHandle.delete(handle);
      }
    }
    if (key === undefined) {
      this.bySource.clear();
    } else {
      this.bySource.delete(key);
    }
  }
}
