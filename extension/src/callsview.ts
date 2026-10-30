/**
 * The recorded history in VS Code: Run Live records each run (level 1) in the run folders the
 * MCP server uses, and the Calls view lists the workspace's runs, Run Live's and agents' alike,
 * with each process's call tree loaded a level at a time, its exceptions and their frames' locals.
 * A call opens in the editor and in the brain; Replay plays a run back on the brain; Show Values
 * opens a call's values. The items come from calls.ts, the queries go through traces.ts.
 *
 * Origin paths (where a value came from, lit on the brain) are a later step: `originHook` is
 * where they plug in.
 */

import { randomBytes } from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import {
  callPageItems,
  ChildrenPage,
  ChildrenRequest,
  EXCEPTIONS,
  exceptionItems,
  HistoryItem,
  moduleFiles,
  PAGE,
  processHead,
  processItems,
  RecordedProcess,
  RunListing,
  runItems,
  shortNode,
  TraceCall,
  TraceException,
  Traceback,
  tracebackItems,
  valueItems,
} from "./calls";
import type { LiveFrame } from "./messages";
import type { ResolvedRun } from "./runs";
import { Traces } from "./traces";
import { CallHistory, valuesBody, valuesHtml } from "./values";

export interface HistoryHooks {
  /** The workspace folders whose runs are listed. */
  roots(): string[];
  /** The brain flies to `node`, showing `root`'s graph first if needed. */
  focus(root: string, node: string): void;
  /** The brain replays the process traced in `file`, from `start` to `end` (ns). */
  replay(root: string, file: string, label: string, start: number, end: number): void;
  /** Where `node` is defined (absolute file, 1-based line), from the graph shown. */
  locate(node: string): { file: string; line: number } | undefined;
  log(line: string): void;
}

/** Runs are listed again at most this often while one is running (new processes, more calls). */
const POLL_MS = 3000;

export class CallsView implements vscode.TreeDataProvider<HistoryItem>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<HistoryItem | undefined>();
  readonly onDidChangeTreeData = this.changed.event;
  readonly traces: Traces;
  view: vscode.TreeView<HistoryItem> | undefined;
  private listings: RunListing[] = [];
  private listed = "";
  /** Children loaded, by parent key ("" for the roots), with "More calls…" replaced as pages come. */
  private readonly loaded = new Map<string, HistoryItem[]>();
  private readonly parents = new Map<string, HistoryItem | undefined>();
  private readonly watchers = new Map<string, fs.FSWatcher>();
  private refreshTimer: NodeJS.Timeout | undefined;
  private poll: NodeJS.Timeout | undefined;
  /** Run Live's runs being recorded: debug sessions by id, and terminals in the order they started. */
  private readonly sessions = new Map<string, number | undefined>();
  private readonly terminalRuns: string[] = [];
  private toldEmpty = false;
  private valuesPanel: vscode.WebviewPanel | undefined;
  private readonly subscriptions: vscode.Disposable[] = [];

  constructor(
    private readonly hooks: HistoryHooks,
    python: () => string,
    analyzerDir: () => string | undefined,
  ) {
    this.traces = new Traces({ python, analyzerDir, log: hooks.log });
    this.subscriptions.push(
      vscode.debug.onDidTerminateDebugSession((session) => {
        const folder = session.configuration.__lucioleRun as string | undefined;
        if (folder) {
          void this.finish(folder, this.sessions.get(session.id));
          this.sessions.delete(session.id);
        }
      }),
      vscode.window.onDidCloseTerminal((terminal) => {
        if (terminal.name === "Luciole live" && this.terminalRuns.length) {
          void this.finish(this.terminalRuns.shift()!, terminal.exitStatus?.code);
        }
      }),
      vscode.workspace.onDidChangeWorkspaceFolders(() => this.refresh()),
    );
    this.refresh();
  }

  dispose(): void {
    this.subscriptions.forEach((s) => s.dispose());
    this.watchers.forEach((watcher) => watcher.close());
    clearTimeout(this.refreshTimer);
    clearTimeout(this.poll);
    this.traces.dispose();
    this.valuesPanel?.dispose();
    this.changed.dispose();
  }

  // Recording

  /**
   * A new run folder for `run` (under `root`), and the variables that make its processes record
   * there at level 1; nothing when the `luciole.recordHistory` setting is off.
   */
  async record(root: string, run: ResolvedRun): Promise<{ folder: string; env: Record<string, string> } | undefined> {
    if (!vscode.workspace.getConfiguration("luciole").get<boolean>("recordHistory", true)) {
      return undefined;
    }
    try {
      const folder = await this.traces.newRun(root, ["python", ...run.target], run.cwd);
      this.hooks.log(`recording the run in ${folder}`);
      return { folder, env: { LUCIOLE_TRACE: folder + path.sep, LUCIOLE_TRACE_LEVEL: "1" } };
    } catch (error) {
      this.hooks.log(`could not make a run folder, so this run is not recorded: ${error}`);
      return undefined;
    }
  }

  /** A terminal Run Live is recording in `folder`: finished when its terminal closes. */
  recordingInTerminal(folder: string): void {
    this.terminalRuns.push(folder);
  }

  /** The debug adapter says how a session's program exited. */
  exited(session: vscode.DebugSession, code: number): void {
    if (session.configuration.__lucioleRun) {
      this.sessions.set(session.id, code);
    }
  }

  private async finish(folder: string, exitStatus: number | undefined): Promise<void> {
    try {
      await this.traces.finishRun(folder, exitStatus);
    } catch (error) {
      this.hooks.log(`could not mark the run in ${folder} as finished: ${error}`);
    }
    await this.refresh();
    const run = this.listings.flatMap((listing) => listing.runs).find((r) => r.folder === folder);
    if (run?.state === "empty" && !this.toldEmpty) {
      this.toldEmpty = true; // once: the run's item says it too
      this.hooks.log("nothing was recorded for this run: recording needs Python 3.12 or newer (sys.monitoring), running project code under the folder");
      vscode.window.setStatusBarMessage("Luciole: nothing recorded (needs Python 3.12+)", 8000);
    }
  }

  // Listing

  /** Lists the runs again; the tree changes only when they did. */
  async refresh(): Promise<void> {
    const roots = this.hooks.roots();
    const listings = (
      await Promise.all(
        roots.map((root) =>
          this.traces.list(root).catch((error) => {
            this.hooks.log(`could not list the runs of ${root}: ${error}`);
            return undefined;
          }),
        ),
      )
    ).filter((listing): listing is RunListing => listing !== undefined);
    for (const listing of listings) {
      this.watch(listing.folder);
    }
    const text = JSON.stringify(listings);
    clearTimeout(this.poll);
    if (listings.some((listing) => listing.runs.some((run) => run.state === "running"))) {
      this.poll = setTimeout(() => void this.refresh(), POLL_MS);
    }
    if (text === this.listed) {
      return;
    }
    this.listed = text;
    this.listings = listings;
    this.loaded.clear();
    this.parents.clear();
    void vscode.commands.executeCommand("setContext", "luciole.hasRuns", listings.some((listing) => listing.runs.length > 0));
    this.changed.fire(undefined);
  }

  /** Runs come and go as folders in `folder`: list again when it changes. */
  private watch(folder: string): void {
    if (this.watchers.has(folder)) {
      return;
    }
    try {
      fs.mkdirSync(folder, { recursive: true });
      const watcher = fs.watch(folder, () => {
        clearTimeout(this.refreshTimer);
        this.refreshTimer = setTimeout(() => void this.refresh(), 500);
      });
      watcher.on("error", () => this.watchers.delete(folder));
      this.watchers.set(folder, watcher);
    } catch (error) {
      this.hooks.log(`cannot watch ${folder}: ${error}`);
    }
  }

  /** The newest run with a recorded process, for the panel's Replay button. */
  latest(): HistoryItem | undefined {
    return runItems(this.listings, new Date()).find((item) => item.kind === "run" && item.run.processes.some((p) => p.frames > 0));
  }

  // Tree

  async getChildren(item?: HistoryItem): Promise<HistoryItem[]> {
    const key = item?.key ?? "";
    const cached = this.loaded.get(key);
    if (cached) {
      return cached;
    }
    let children: HistoryItem[];
    try {
      children = await this.load(item);
    } catch (error) {
      children = [{ kind: "note", key: `note:${key}:error`, label: "Could not read the trace", description: String(error instanceof Error ? error.message : error), expandable: false, root: item?.root ?? "" }];
    }
    this.loaded.set(key, children);
    children.forEach((child) => this.parents.set(child.key, item));
    return children;
  }

  private async load(item: HistoryItem | undefined): Promise<HistoryItem[]> {
    if (!item) {
      return runItems(this.listings, new Date());
    }
    switch (item.kind) {
      case "run":
        // One process (the usual case): its content right under the run.
        return item.run.processes.length === 1 ? this.processContent(item.run.processes[0].file, item.root, item.run.processes[0]) : processItems(item.run, item.root);
      case "process":
        return this.processContent(item.process.file, item.root, item.process);
      case "call":
        return item.children ? this.page(item.file, item.root, item.children) : [];
      case "exceptions": {
        const found = await this.traces.query<{ total: number; exceptions: TraceException[] }>(item.file, "exceptions", { limit: EXCEPTIONS });
        return exceptionItems(found.exceptions, found.total, item.file, item.root);
      }
      case "exception": {
        const traceback = await this.traces.query<Traceback | null>(item.file, "traceback", { exception: item.exception.id });
        return traceback ? tracebackItems(traceback, item.file, item.root) : [];
      }
      case "where":
        return valueItems(item.call.locals ?? {}, item.key, item.root);
      default:
        return [];
    }
  }

  private async processContent(file: string, root: string, process: RecordedProcess): Promise<HistoryItem[]> {
    return [...processHead(process, root), ...(await this.page(file, root, { frame: null, after: 0, collapse: true }))];
  }

  private async page(file: string, root: string, request: ChildrenRequest): Promise<HistoryItem[]> {
    const page = await this.traces.query<ChildrenPage>(file, "children", { ...request, limit: PAGE });
    return callPageItems(page, request, file, root);
  }

  /** "More calls…": the next page takes its place. */
  async loadMore(item: HistoryItem): Promise<void> {
    if (item.kind !== "more") {
      return;
    }
    const parent = this.parents.get(item.key);
    const siblings = this.loaded.get(parent?.key ?? "");
    if (!siblings) {
      return;
    }
    const next = await this.page(item.file, item.root, item.request);
    const at = siblings.indexOf(item);
    if (at >= 0) {
      siblings.splice(at, 1, ...next);
      next.forEach((child) => this.parents.set(child.key, parent));
      this.changed.fire(parent);
    }
  }

  getParent(item: HistoryItem): HistoryItem | undefined {
    return this.parents.get(item.key);
  }

  getTreeItem(item: HistoryItem): vscode.TreeItem {
    const tree = new vscode.TreeItem(item.label, item.expandable ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None);
    tree.id = item.key;
    tree.description = item.description;
    tree.tooltip = item.tooltip;
    tree.contextValue = item.kind === "run" && !item.run.processes.length ? "run.empty" : item.kind;
    if (item.icon) {
      tree.iconPath = new vscode.ThemeIcon(item.icon, item.error ? new vscode.ThemeColor("errorForeground") : undefined);
    }
    if (item.kind === "call" || item.kind === "where") {
      tree.command = { command: "luciole.showCall", title: "Show Call", arguments: [item] };
    } else if (item.kind === "more") {
      tree.command = { command: "luciole.loadMore", title: "Load More", arguments: [item] };
    }
    return tree;
  }

  // Actions

  /** A call: the brain flies to its function, the editor opens where it was called (or where it is). */
  showCall(item: HistoryItem): void {
    if (item.kind !== "call" && item.kind !== "where") {
      return;
    }
    const call = item.call;
    this.hooks.focus(item.root, call.node);
    // A traceback frame: the line it was at. A call: the line that called it, else the function.
    const at = item.kind === "where" ? this.place(item.root, call.node, call.at_line) : call.caller?.node && call.caller.line ? this.place(item.root, call.caller.node, call.caller.line) : this.place(item.root, call.node);
    if (at) {
      const position = new vscode.Position(Math.max(at.line - 1, 0), 0);
      void vscode.window.showTextDocument(vscode.Uri.file(at.file), { viewColumn: vscode.ViewColumn.One, preserveFocus: true, selection: new vscode.Range(position, position) });
    }
  }

  /** `node`'s file, at `line` or where it is defined; from the graph, else from its module's name. */
  private place(root: string, node: string, line?: number): { file: string; line: number } | undefined {
    const found = this.hooks.locate(node);
    if (found) {
      return { file: found.file, line: line ?? found.line };
    }
    const file = moduleFiles(root, node).find((candidate) => fs.existsSync(candidate));
    return file ? { file, line: line ?? 1 } : undefined;
  }

  /** Replays a run (its first process) or a process on the brain. */
  replay(item: HistoryItem | undefined): void {
    const target = item ?? this.latest();
    if (!target) {
      void vscode.window.showInformationMessage("Luciole: no recorded run yet. Run Live records each run (setting luciole.recordHistory).");
      return;
    }
    const process = target.kind === "process" ? target.process : target.kind === "run" ? target.run.processes.find((p) => p.frames > 0) : undefined;
    if (!process) {
      void vscode.window.showInformationMessage("Luciole: this run recorded no calls to replay.");
      return;
    }
    const label = target.kind === "run" ? target.label : `${target.label} (${process.pid})`;
    this.hooks.replay(target.root, process.file, label, process.first_ns, process.duration_ns);
  }

  /** Frames for the replay of `file`, as the panel's timeline asks for them. */
  frames(file: string, from: number, to: number, every: number): Promise<LiveFrame[]> {
    return this.traces.query<LiveFrame[]>(file, "frames", { from: Math.round(from), to: Math.round(to), every: Math.max(1, Math.round(every)) });
  }

  /** A call's values, in a read-only page beside the editor. */
  async showValues(item: HistoryItem): Promise<void> {
    if (item.kind !== "call" && item.kind !== "where") {
      return;
    }
    const [call, history] = await Promise.all([
      this.traces.query<TraceCall | null>(item.file, "frame", { frame: item.call.frame }),
      this.traces.query<CallHistory>(item.file, "history", { frame: item.call.frame, limit: 500 }).catch((error: Error) => error),
    ]);
    if (!call) {
      return;
    }
    const body = valuesBody({
      call,
      history: history instanceof Error ? undefined : history,
      problem: history instanceof Error ? history.message : undefined,
      scopeHint: `luciole run --trace DIR --scope ${shortNode(call.node)} …`,
    });
    const panel = (this.valuesPanel ??= this.makeValuesPanel());
    panel.title = `Values: ${shortNode(call.node)} #${call.frame}`;
    const nonce = randomBytes(16).toString("hex");
    panel.webview.html = valuesHtml(body, nonce, panel.webview.cspSource);
    this.valuesCall = { root: item.root, call };
    panel.reveal(vscode.ViewColumn.Beside, true);
  }

  private valuesCall: { root: string; call: TraceCall } | undefined;

  private makeValuesPanel(): vscode.WebviewPanel {
    const panel = vscode.window.createWebviewPanel("luciole.values", "Values", { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true }, { enableScripts: true });
    panel.onDidDispose(() => (this.valuesPanel = undefined));
    panel.webview.onDidReceiveMessage((message: { type: string; line: number }) => {
      const shown = this.valuesCall;
      if (!shown) {
        return;
      }
      const node = message.type === "openCaller" ? shown.call.caller?.node : shown.call.node;
      const at = node ? this.place(shown.root, node, message.line) : undefined;
      if (at) {
        const position = new vscode.Position(Math.max(at.line - 1, 0), 0);
        void vscode.window.showTextDocument(vscode.Uri.file(at.file), { viewColumn: vscode.ViewColumn.One, selection: new vscode.Range(position, position) });
      }
    });
    return panel;
  }
}

/**
 * Where origin queries (phase 3, step 5: `src/luciole/origins.py`) will plug into the view: given
 * a call and one of its values, the path that value took, to list under the call and light on
 * the brain. Not built yet.
 */
export type OriginHook = (file: string, frame: number, name: string) => Promise<{ node: string; frame: number; line: number }[]>;
