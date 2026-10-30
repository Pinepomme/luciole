/**
 * The Luciole view container in the activity bar: Actions (commands and the auto-start switch),
 * Running (the processes lit up now) and Map (an outline of the graph in the panel). The items
 * come from actions.ts, running.ts and map.ts; this file only turns them into VS Code tree items.
 */

import * as path from "path";
import * as vscode from "vscode";
import { ActionItem, actionItems } from "./actions";
import type { Graph } from "./graph";
import type { LiveSession } from "./live";
import { MapEntry, MapTree, buildMap, mapDescription, mapFunctions, runningCounts } from "./map";
import { ProcessItem, processItems } from "./running";

export class ActionsView implements vscode.TreeDataProvider<ActionItem>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;

  refresh(): void {
    this.changed.fire();
  }

  dispose(): void {
    this.changed.dispose();
  }

  getChildren(): ActionItem[] {
    return actionItems(vscode.workspace.getConfiguration("luciole").get<boolean>("autoStart", false));
  }

  getTreeItem(action: ActionItem): vscode.TreeItem {
    const item = new vscode.TreeItem(action.label);
    item.iconPath = new vscode.ThemeIcon(action.icon);
    item.description = action.description;
    item.tooltip = action.tooltip;
    item.command = { command: action.command, title: action.label };
    return item;
  }
}

export class RunningView implements vscode.TreeDataProvider<ProcessItem>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private readonly subscription: vscode.Disposable;

  constructor(private readonly live: LiveSession) {
    this.subscription = live.onDidChangeProcesses(() => {
      void vscode.commands.executeCommand("setContext", "luciole.exitedProcesses", live.running.list.some((p) => p.exited));
      this.changed.fire();
    });
  }

  dispose(): void {
    this.subscription.dispose();
    this.changed.dispose();
  }

  getChildren(): ProcessItem[] {
    return processItems(
      this.live.running.list,
      (process) => this.live.isDebugged(process),
      (process) => this.live.isStopping(process),
    );
  }

  getTreeItem(process: ProcessItem): vscode.TreeItem {
    const item = new vscode.TreeItem(process.label);
    item.id = `process:${process.id}:${process.exited ? "exited" : "running"}`;
    item.description = process.description;
    item.tooltip = process.tooltip;
    item.contextValue = `process.${process.action}`;
    item.iconPath = process.exited
      ? new vscode.ThemeIcon("circle-outline", new vscode.ThemeColor("disabledForeground"))
      : new vscode.ThemeIcon(process.action === "debug" ? "debug-alt" : "pulse", new vscode.ThemeColor("textLink.foreground"));
    return item;
  }
}

const ICONS = { package: "package", module: "symbol-file", class: "symbol-class", function: "symbol-function" } as const;
/** At most this many tree refreshes per second while functions start and stop running. */
const RUNNING_REFRESH_MS = 300;

export class MapView implements vscode.TreeDataProvider<MapEntry>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private tree: MapTree | undefined;
  /** The graph's root folder, for paths in the tree. */
  root: string | undefined;
  private running = new Set<string>();
  private counts = new Map<string, number>();
  private refreshTimer: NodeJS.Timeout | undefined;
  view: vscode.TreeView<MapEntry> | undefined;

  /** The panel shows `graph` (of `target`), or no graph (undefined). */
  setGraph(graph: Graph | undefined, target?: string): void {
    this.tree = graph ? buildMap(graph) : undefined;
    this.root = graph?.root;
    this.places = new Map(
      (graph?.nodes ?? []).filter((node) => node.kind === "function" || node.kind === "module").map((node) => [node.id, { file: path.resolve(graph!.root, node.file), line: node.loc.line }]),
    );
    this.counts = this.tree ? runningCounts(this.tree, this.running) : new Map();
    if (this.view) {
      this.view.message = undefined;
      this.view.description = target;
    }
    this.changed.fire();
  }

  /** The panel is analyzing `target`. */
  loading(target: string): void {
    if (this.view && !this.tree) {
      this.view.message = `Analyzing ${target}…`;
    }
  }

  /** The analyzer failed: the panel tells why. */
  failed(): void {
    if (this.view) {
      this.view.message = "No graph: the analyzer failed (see the Luciole panel).";
    }
  }

  /** The neurons on the live stacks now; the tree marks them, a few times a second at most. */
  setRunning(ids: Iterable<string>): void {
    const running = new Set(ids);
    if (running.size === this.running.size && [...running].every((id) => this.running.has(id))) {
      return;
    }
    this.running = running;
    if (!this.refreshTimer) {
      this.refreshTimer = setTimeout(() => {
        this.refreshTimer = undefined;
        this.counts = this.tree ? runningCounts(this.tree, this.running) : new Map();
        this.changed.fire();
      }, RUNNING_REFRESH_MS);
    }
  }

  dispose(): void {
    clearTimeout(this.refreshTimer);
    this.refreshTimer = undefined;
    this.changed.dispose();
  }

  get functions(): MapEntry[] {
    return this.tree ? mapFunctions(this.tree) : [];
  }

  /** Where the graph shown defines `node` (a function, or a module's code): absolute file, 1-based line. */
  locate(node: string): { file: string; line: number } | undefined {
    return this.places.get(node);
  }

  private places = new Map<string, { file: string; line: number }>();

  getChildren(entry?: MapEntry): MapEntry[] {
    if (!this.tree) {
      return [];
    }
    return (entry ? entry.children : this.tree.roots).map((key) => this.tree!.entries.get(key)!);
  }

  getParent(entry: MapEntry): MapEntry | undefined {
    return entry.parent !== undefined ? this.tree?.entries.get(entry.parent) : undefined;
  }

  /** How many items share `entry`'s parent, itself included. */
  private siblingCount(entry: MapEntry): number {
    const parent = this.getParent(entry);
    return parent ? parent.children.length : (this.tree?.roots.length ?? 0);
  }

  getTreeItem(entry: MapEntry): vscode.TreeItem {
    const running = this.counts.get(entry.key) ?? 0;
    const collapsible = entry.children.length
      ? // Open what holds a single region, so a file's graph opens on its functions.
        this.siblingCount(entry) === 1
        ? vscode.TreeItemCollapsibleState.Expanded
        : vscode.TreeItemCollapsibleState.Collapsed
      : vscode.TreeItemCollapsibleState.None;
    const item = new vscode.TreeItem(entry.label, collapsible);
    item.id = entry.key;
    item.description = mapDescription(entry, running);
    item.iconPath = new vscode.ThemeIcon(ICONS[entry.kind], running ? new vscode.ThemeColor("textLink.foreground") : undefined);
    item.contextValue = entry.kind === "function" ? "function" : "region";
    item.tooltip = entry.kind === "function" ? `${entry.container ? `${entry.container}.` : ""}${entry.label}\n${entry.file}:${entry.line}` : `${entry.kind} ${entry.label}`;
    item.command = { command: "luciole.revealInBrain", title: "Show in Brain", arguments: [entry] };
    return item;
  }
}
