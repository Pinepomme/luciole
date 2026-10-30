/**
 * The Calls view's items: the recorded runs of the workspace (Run Live's and agents'), their
 * processes, each process's call tree a level at a time, and its exceptions with the frames they
 * went through. Built from what `luciole runs --json` and `luciole trace FILE serve` answer
 * (src/luciole/runs.py, browse.py). Pure logic (no VS Code).
 */

import * as path from "path";
import { commandLine } from "./running";

/** A value as the trace recorded it (docs/trace-format.md, "snapshots"). */
export interface Snapshot {
  type: string;
  repr: string;
  items?: { len: number; items: unknown[] };
  truncated?: boolean;
}

/** One call, as Trace.frame() and Trace.children() give it. */
export interface TraceCall {
  frame: number;
  node: string;
  caller: { frame: number; node: string | null; line: number | null } | null;
  origin: number | null;
  thread: string | null;
  start_ns: number;
  duration_ns: number | null;
  end: string | null;
  exception: { id: number; type?: string; message?: string; frame?: number } | null;
  args?: Record<string, Snapshot | null>;
  result?: Snapshot | null;
  locals?: Record<string, Snapshot | null>;
  /** children(): consecutive calls this item stands for, the last one's frame, and the calls below. */
  count?: number;
  last?: number;
  calls?: number;
  /** traceback(): the line it was at when the exception left it. */
  at_line?: number;
}

export interface ChildrenPage {
  calls: TraceCall[];
  /** Where the next page starts (`after`), null at the end. */
  next: number | null;
}

/** What to ask `children` for: the calls `frame` made (null: the roots), past `after`, up to `until`. */
export interface ChildrenRequest {
  frame: number | null;
  after: number;
  until?: number;
  collapse: boolean;
}

export interface RecordedProcess {
  file: string;
  pid: number;
  argv: string[];
  started_at: number;
  duration_ns: number;
  ended: boolean;
  exit_status: number | null;
  ended_by: string | null;
  frames: number;
  exceptions: number;
  uncaught: number;
  level: number;
  scope: string[] | null;
  truncated: boolean;
  /** When its first call started (ns since it started): where a replay begins. */
  first_ns: number;
}

export interface RecordedRun {
  id: string;
  folder: string;
  command: string[];
  cwd: string;
  level: number;
  scope: string[] | null;
  /** Who started it: "vscode" (Run Live) or "agent" (`luciole mcp`). */
  by: string;
  started_at: number;
  exit_status?: number | null;
  state: "running" | "finished" | "empty";
  processes: RecordedProcess[];
}

export interface RunListing {
  root: string;
  folder: string;
  runs: RecordedRun[];
}

export interface TraceException {
  id: number;
  frame: number | null;
  line: number | null;
  type: string;
  message: string;
  cause: number | null;
  chain: string | null;
  time_ns: number;
  handled_in: number | null;
  node: string | null;
  handled_node: string | null;
}

export interface Traceback {
  exception: TraceException;
  frames: TraceCall[];
  handled_in: TraceCall | null;
}

interface Base {
  /** Unique in the tree, and stable across refreshes (the tree keeps what was open). */
  key: string;
  label: string;
  description?: string;
  tooltip?: string;
  /** A codicon name; `error` items are drawn in the error color. */
  icon?: string;
  error?: boolean;
  expandable: boolean;
  /** The project folder the run is of (node ids are relative to it). */
  root: string;
}

export type HistoryItem =
  | (Base & { kind: "run"; run: RecordedRun })
  | (Base & { kind: "process"; run: RecordedRun; process: RecordedProcess })
  | (Base & { kind: "call"; file: string; call: TraceCall; children?: ChildrenRequest })
  | (Base & { kind: "more"; file: string; request: ChildrenRequest })
  | (Base & { kind: "exceptions"; file: string })
  | (Base & { kind: "exception"; file: string; exception: TraceException })
  | (Base & { kind: "where"; file: string; call: TraceCall })
  | (Base & { kind: "value" })
  | (Base & { kind: "note" });

/** Calls asked for per page, and exceptions listed per process. */
export const PAGE = 100;
export const EXCEPTIONS = 200;

/** `Cart.add` for `shop:Cart.add`, the module for its body (`pkg.mod`). */
export function shortNode(node: string): string {
  const colon = node.indexOf(":");
  return colon >= 0 ? node.slice(colon + 1) : node;
}

export function durationText(ns: number | null | undefined): string {
  if (ns === null || ns === undefined) {
    return "?";
  }
  if (ns < 1_000) {
    return `${ns} ns`;
  }
  if (ns < 1_000_000) {
    return `${(ns / 1_000).toFixed(1)} µs`;
  }
  if (ns < 1_000_000_000) {
    return `${(ns / 1_000_000).toFixed(1)} ms`;
  }
  return `${(ns / 1_000_000_000).toFixed(2)} s`;
}

function clip(text: string, max: number): string {
  const line = text.replace(/\s+/g, " ");
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

export function valueText(value: Snapshot | null | undefined, max = 60): string {
  return value ? clip(value.repr, max) : "?";
}

/** `(price=10, n=2)`, without `self` (an object's identity says little in a list). */
export function argsText(args: Record<string, Snapshot | null> | undefined, max = 80): string {
  if (!args) {
    return "";
  }
  const shown = Object.entries(args).filter(([name]) => name !== "self");
  return `(${clip(shown.map(([name, value]) => `${name}=${valueText(value, 40)}`).join(", "), max)})`;
}

/** What a call gave back: `→ 18.0`, `✕ ValueError: bad`, `↑ ValueError` (passed through), or how it ended. */
export function outcomeText(call: TraceCall): string {
  if (call.end === "return") {
    return call.result !== undefined ? `→ ${valueText(call.result, 50)}` : "";
  }
  if ((call.end === "raise" || call.end === "unwind") && call.exception) {
    const type = call.exception.type ?? "exception";
    return call.end === "raise" ? `✕ ${type}: ${clip(call.exception.message ?? "", 50)}` : `↑ ${type}`;
  }
  return call.end ? `[${call.end}]` : "[running]";
}

/** A call raised, or an exception went through it. */
export function raised(call: TraceCall): boolean {
  return call.end === "raise" || call.end === "unwind";
}

/** Time of day for runs of today, else the date too. */
export function runTime(startedAt: number, now: Date): string {
  const date = new Date(startedAt * 1000);
  const time = date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  if (date.toDateString() === now.toDateString()) {
    return time;
  }
  return `${date.toLocaleDateString("en-US", { month: "short", day: "numeric" })} ${time}`;
}

/** The command as typed: `pytest -x tests`, `shop.py --flag` (a script by its file name). */
export function runCommand(command: string[]): string {
  if (!command.length) {
    return "python";
  }
  if (/^python[\d.]*(\.exe)?$/i.test(path.basename(command[0]))) {
    return commandLine(command);
  }
  return [path.basename(command[0]), ...command.slice(1)].join(" ");
}

function runStatus(run: RecordedRun): string {
  if (run.state === "running") {
    return "running";
  }
  if (run.state === "empty") {
    return "nothing recorded";
  }
  return run.exit_status === null || run.exit_status === undefined ? "finished" : `exit ${run.exit_status}`;
}

/** The runs of every workspace folder, newest first. */
export function runItems(listings: RunListing[], now: Date): HistoryItem[] {
  const runs = listings.flatMap((listing) => listing.runs.map((run) => ({ run, root: listing.root })));
  runs.sort((a, b) => b.run.started_at - a.run.started_at || (a.run.id < b.run.id ? 1 : -1));
  return runs.map(({ run, root }) => {
    const failed = run.state === "finished" && Boolean(run.exit_status) || run.processes.some((p) => p.uncaught > 0);
    const status = runStatus(run);
    const agent = run.by === "agent";
    const empty = run.state === "empty";
    return {
      kind: "run",
      key: `run:${run.folder}`,
      label: runCommand(run.command),
      description: [runTime(run.started_at, now), status, agent ? "agent" : ""].filter(Boolean).join(" · "),
      tooltip: [
        `${run.command.join(" ")}`,
        `in ${run.cwd}`,
        `${agent ? "run by an agent (luciole mcp)" : "Run Live"}, level ${run.level}${run.scope?.length ? ` (variables of ${run.scope.join(", ")})` : ""}`,
        `${status}, ${run.processes.length} process${run.processes.length === 1 ? "" : "es"} recorded`,
        empty ? "Nothing was recorded: recording needs Python 3.12 or newer, and project code under the folder." : "",
      ]
        .filter(Boolean)
        .join("\n"),
      icon: run.state === "running" ? "pulse" : failed ? "error" : agent ? "hubot" : "history",
      error: failed,
      expandable: run.processes.length > 0,
      root,
      run,
    };
  });
}

/** A run's processes; with a single one, its content directly (see processChildren). */
export function processItems(run: RecordedRun, root: string): HistoryItem[] {
  return run.processes.map((process) => {
    const exceptions = process.uncaught ? `${process.uncaught} uncaught` : process.exceptions ? `${process.exceptions} exceptions` : "";
    const state = process.ended ? durationText(process.duration_ns) : "running";
    return {
      kind: "process",
      key: `process:${process.file}`,
      label: commandLine(process.argv),
      description: [`pid ${process.pid}`, `${process.frames} calls`, state, exceptions].filter(Boolean).join(" · "),
      tooltip: `${process.argv.join(" ")}\npid ${process.pid}, level ${process.level}${process.truncated ? ", truncated" : ""}\n${process.file}`,
      icon: process.uncaught ? "error" : "terminal",
      error: process.uncaught > 0,
      expandable: true,
      root,
      run,
      process,
    };
  });
}

/** What a process holds: its exceptions (when there are some), then the roots of its call tree. */
export function processHead(process: RecordedProcess, root: string): HistoryItem[] {
  if (!process.exceptions) {
    return [];
  }
  return [
    {
      kind: "exceptions",
      key: `exceptions:${process.file}`,
      label: "Exceptions",
      description: process.uncaught ? `${process.exceptions} · ${process.uncaught} uncaught` : String(process.exceptions),
      icon: process.uncaught ? "error" : "warning",
      error: process.uncaught > 0,
      expandable: true,
      root,
      file: process.file,
    },
  ];
}

/** The first page of calls below `call`, or the calls a collapsed `×N` item stands for. */
export function callChildren(call: TraceCall): ChildrenRequest | undefined {
  if ((call.count ?? 1) > 1) {
    return { frame: call.caller?.frame ?? null, after: call.frame - 1, until: call.last, collapse: false };
  }
  return call.calls ? { frame: call.frame, after: 0, collapse: true } : undefined;
}

export function callItem(call: TraceCall, file: string, root: string): HistoryItem {
  const count = call.count ?? 1;
  const children = callChildren(call);
  const outcome = outcomeText(call);
  const name = shortNode(call.node);
  const where = call.caller ? `called by ${shortNode(call.caller.node ?? "?")} at line ${call.caller.line}` : call.origin ? `started by #${call.origin}` : "a root";
  const lines = [
    `${name}${argsText(call.args, 400)} ${outcome}`.trim(),
    count > 1 ? `${count} calls in a row (#${call.frame} to #${call.last}), ${durationText(call.duration_ns)} in all` : `#${call.frame}, ${durationText(call.duration_ns)}, ${where}, thread ${call.thread}`,
    call.calls ? `${call.calls} calls below` : "",
  ];
  return {
    kind: "call",
    key: `call:${file}:${call.frame}:${count}`,
    label: count > 1 ? `${name} ×${count}` : name,
    // A module's code has no arguments, and returns None: what it raised is all there is to say.
    description: [call.node.includes(":") ? `${argsText(call.args)} ${outcome}`.trim() : raised(call) ? outcome : "", durationText(call.duration_ns)].filter(Boolean).join(" · "),
    tooltip: lines.filter(Boolean).join("\n"),
    icon: raised(call) ? "error" : count > 1 ? "layers" : "symbol-function",
    error: raised(call),
    expandable: children !== undefined,
    root,
    file,
    call,
    children,
  };
}

/** A page of calls, and an item to load the next one when there is more. */
export function callPageItems(page: ChildrenPage, request: ChildrenRequest, file: string, root: string): HistoryItem[] {
  const items = page.calls.map((call) => callItem(call, file, root));
  if (page.next !== null) {
    items.push({
      kind: "more",
      key: `more:${file}:${request.frame}:${page.next}:${request.until ?? ""}`,
      label: "More calls…",
      tooltip: "Load the next calls",
      icon: "ellipsis",
      expandable: false,
      root,
      file,
      request: { ...request, after: page.next },
    });
  }
  return items;
}

export function exceptionItems(exceptions: TraceException[], total: number, file: string, root: string): HistoryItem[] {
  const items: HistoryItem[] = exceptions.map((e) => ({
    kind: "exception",
    key: `exception:${file}:${e.id}`,
    label: `${e.type}: ${clip(e.message, 80)}`,
    description: [e.node ? `${shortNode(e.node)}:${e.line}` : `line ${e.line}`, e.handled_in ? `handled in ${shortNode(e.handled_node ?? "?")}` : "not handled"].join(" · "),
    tooltip: `${e.type}: ${e.message}\nraised in ${e.node ?? "?"} at line ${e.line}${e.cause ? `\n${e.chain === "cause" ? "caused by" : "while handling"} !${e.cause}` : ""}\n${e.handled_in ? `handled in ${e.handled_node} (#${e.handled_in})` : "not handled"}`,
    icon: e.handled_in ? "warning" : "error",
    error: !e.handled_in,
    expandable: e.frame !== null,
    root,
    file,
    exception: e,
  }));
  if (total > exceptions.length) {
    items.push({ kind: "note", key: `note:${file}:exceptions`, label: `${total - exceptions.length} more exceptions`, description: "luciole trace FILE exceptions --offset …", expandable: false, root });
  }
  return items;
}

/** The frames an exception went through, innermost first, then where it was handled. */
export function tracebackItems(traceback: Traceback, file: string, root: string): HistoryItem[] {
  const frames = traceback.frames.map((call, i) => whereItem(call, file, root, `${traceback.exception.id}:${i}`, call.end === "raise" ? "raised here" : "passed through"));
  if (traceback.handled_in) {
    frames.push(whereItem(traceback.handled_in, file, root, `${traceback.exception.id}:handled`, "handled here"));
  }
  return frames;
}

function whereItem(call: TraceCall, file: string, root: string, key: string, what: string): HistoryItem {
  const locals = Object.keys(call.locals ?? {}).length;
  return {
    kind: "where",
    key: `where:${file}:${key}`,
    label: shortNode(call.node),
    description: `line ${call.at_line} · ${what}`,
    tooltip: `${call.node} (#${call.frame}), line ${call.at_line}: ${what}${locals ? `\n${locals} local variables as the exception left it` : ""}`,
    icon: what === "handled here" ? "pass" : "debug-stackframe",
    expandable: locals > 0,
    root,
    file,
    call,
  };
}

/** Values by name: a frame's locals, or a call's arguments. */
export function valueItems(values: Record<string, Snapshot | null>, keyPrefix: string, root: string): HistoryItem[] {
  return Object.entries(values).map(([name, value]) => ({
    kind: "value",
    key: `value:${keyPrefix}:${name}`,
    label: name,
    description: valueText(value, 120),
    tooltip: value ? `${name}: ${value.type}\n${value.repr}${value.truncated ? "\n(cut)" : ""}` : name,
    icon: "symbol-variable",
    expandable: false,
    root,
  }));
}

/**
 * Files that may hold the code of `node` under `root`, for nodes not in the graph shown:
 * `pkg.mod:f` is in `pkg/mod.py` or `pkg/mod/__init__.py`, also under `src/`.
 */
export function moduleFiles(root: string, node: string): string[] {
  const module = node.split(":")[0];
  const parts = module.split(".");
  return ["", "src"].flatMap((base) => [path.join(root, base, ...parts) + ".py", path.join(root, base, ...parts, "__init__.py")]);
}
