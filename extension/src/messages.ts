/** Messages between the extension host and the graph webview. */

import type { Graph } from "./graph";

export interface ThreadStack {
  thread: string;
  /** Project frames, outermost first. */
  stack: string[];
}

/** One line of `luciole run` output (see "Live events" in docs/graph-format.md). */
export interface LiveFrame {
  t?: number;
  /** Each thread's project stack, main thread first. */
  stacks?: ThreadStack[];
  /** Calls per function since the previous frame. */
  calls?: Record<string, number>;
  /** Calls per `caller>callee` pair since the previous frame. */
  links?: Record<string, number>;
  /** Threads and asyncio tasks started, per `origin>function`, since the previous frame. */
  spawns?: Record<string, number>;
  /** Coroutines suspended on an await right now, per function. */
  waiting?: Record<string, number>;
  done?: boolean;
}

/** A value in a paused program; `ref` asks the recorder for its children (items, attributes). */
export interface Variable {
  name: string;
  type?: string;
  value: string;
  ref?: number;
}

export interface PausedFrame {
  /** Graph node id of the function (or module). */
  node: string;
  name: string;
  /** Absolute path. */
  file: string;
  line: number;
  vars: Variable[];
}

/** A thread stopped by a pause, with its project frames, innermost first. */
export interface PausedThread {
  thread: string;
  id: number;
  frames: PausedFrame[];
}

/** Messages the recorder sends to the view only while paused (see recorder.py). */
export interface RecorderReply {
  paused?: PausedThread;
  children?: { ref: number; vars: Variable[] };
}

export type StepMode = "over" | "into" | "out";

export interface LiveState {
  /** A live run's process is alive. */
  running: boolean;
  /** Pause was asked: threads stop at their next line of project code until resumed. */
  paused: boolean;
  /** Threads stopped so far. */
  held: number;
  /** A step is running: everything stops again when the stepping thread reaches its line. */
  stepping: boolean;
  /** Stop was asked once already: stopping again kills the process. */
  stopping: boolean;
}

export type ToWebview =
  | { type: "loading"; target: string }
  | { type: "graph"; target: string; graph: Graph; warnings: string[] }
  | { type: "error"; message: string }
  | { type: "live"; frame: LiveFrame }
  | { type: "liveStarted"; script: string }
  | { type: "liveState"; state: LiveState }
  | { type: "focus"; node: string };

export type FromWebview =
  | { type: "ready" }
  | { type: "refresh" }
  | { type: "runLive" }
  | { type: "runLiveWith" }
  | { type: "stopLive" }
  | { type: "pauseLive"; paused: boolean }
  | { type: "step"; mode: StepMode }
  | { type: "reveal"; file: string; line: number; col: number };
