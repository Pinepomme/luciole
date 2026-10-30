/** Messages between the extension host and the graph webview. */

import type { Graph } from "./graph";
import type { Filters } from "./webview/filters";

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

export interface LiveState {
  /** A live run's process is alive (or its debug session runs). */
  running: boolean;
  /** Stop was asked once already: stopping a terminal run again kills the process. */
  stopping: boolean;
  /** The run is under VS Code's debugger: its toolbar stops it, so the panel shows no Stop. */
  debugged: boolean;
}

export type ToWebview =
  | { type: "loading"; target: string }
  | { type: "graph"; target: string; graph: Graph; warnings: string[] }
  | { type: "error"; message: string }
  | { type: "live"; frame: LiveFrame }
  | { type: "liveStarted"; script: string }
  | { type: "liveState"; state: LiveState }
  /** Run Live ran without the Python debugger (in a terminal): offer to install it, until it is. */
  | { type: "debuggerNotice"; show: boolean }
  /** VS Code's breakpoints in the graph's files (paths relative to its root). */
  | { type: "breakpoints"; breakpoints: SourceLine[] }
  /** The debugger stopped there, or went on (undefined). */
  | { type: "debugStopped"; at: SourceLine | undefined }
  /** The Map view chose a neuron (a function, or module code), or a region (`pkg:`, `mod:`, `cls:` id): fly there and mark it. */
  | { type: "focus"; node: string }
  | { type: "focusRegion"; region: string }
  /** Replay a recorded process from `start` to `end` (ns since it started): the panel shows a timeline. */
  | { type: "replayStart"; label: string; start: number; end: number }
  /** Frames asked for with `replayFetch` (live frames made from the trace), or why there are none. */
  | { type: "replayFrames"; seq: number; frames: LiveFrame[] }
  | { type: "replayFailed"; seq: number; message: string }
  | { type: "replayStop" }
  /** Focus and filters: what stands out, what is dimmed or hidden. */
  | { type: "filters"; filters: Filters };

/** A line of a file, relative to the graph's root. */
export interface SourceLine {
  file: string;
  line: number;
}

export type FromWebview =
  | { type: "ready" }
  | { type: "refresh" }
  | { type: "runLive" }
  | { type: "runLiveWith" }
  | { type: "stopLive" }
  /** The notice of a run without the Python debugger: install it. */
  | { type: "installDebugger" }
  | { type: "reveal"; file: string; line: number; col: number }
  /** Right-click on a neuron: a breakpoint where each call of the function starts. */
  | { type: "addBreakpoint"; file: string; line: number }
  /** Right-click on a neuron that has breakpoints: remove them all. */
  | { type: "removeBreakpoints"; file: string; lines: number[] }
  /** The replay's timeline needs frames from `from` to `to` (ns), one per `every`. */
  | { type: "replayFetch"; seq: number; from: number; to: number; every: number }
  /** The panel's Replay button: the newest recorded run. */
  | { type: "replayLatest" }
  | { type: "replayClosed" }
  | { type: "clearFilters" };
