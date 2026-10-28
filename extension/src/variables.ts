/**
 * The Variables view: while a live run is paused, each stopped thread with its project frames,
 * and each frame's variables, expanded on demand like VS Code's debugger. The line each thread
 * stopped on is highlighted in the editor.
 *
 * Every step brings fresh snapshots, with new refs. What the user expanded or collapsed is kept
 * by path (thread, frame depth from the outermost, variable names), so a step reopens the same
 * variables in the new snapshot.
 */

import * as vscode from "vscode";
import type { PausedFrame, PausedThread, Variable } from "./messages";

type Item = { key: string } & (
  | { kind: "thread"; thread: PausedThread }
  | { kind: "frame"; frame: PausedFrame; thread: PausedThread; top: boolean }
  | { kind: "variable"; variable: Variable }
);

export class VariablesView implements vscode.TreeDataProvider<Item>, vscode.Disposable {
  private threads: PausedThread[] = [];
  /** A step is running: the snapshots shown are replaced when the first thread stops again. */
  private stale = false;
  /** Expanded (true) or collapsed (false) by the user, by item key. */
  private readonly expanded = new Map<string, boolean>();
  /** Bumped with every snapshot, so tree item ids change and VS Code takes our expansion states. */
  private generation = 0;
  /** Name of the thread whose frame was shown last: the one a step applies to. */
  private focused: string | undefined;
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private readonly stoppedLine = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: new vscode.ThemeColor("editor.stackFrameHighlightBackground"),
    overviewRulerColor: new vscode.ThemeColor("editor.stackFrameHighlightBackground"),
    overviewRulerLane: vscode.OverviewRulerLane.Full,
  });
  private readonly subscriptions: vscode.Disposable[];

  constructor(private readonly expand: (ref: number) => Promise<Variable[]>) {
    const view = vscode.window.createTreeView("luciole.variables", { treeDataProvider: this });
    this.subscriptions = [
      this.changed,
      this.stoppedLine,
      view,
      view.onDidExpandElement(({ element }) => this.expanded.set(element.key, true)),
      view.onDidCollapseElement(({ element }) => this.expanded.set(element.key, false)),
      vscode.window.onDidChangeVisibleTextEditors(() => this.decorate()),
    ];
  }

  get isEmpty(): boolean {
    return this.threads.length === 0;
  }

  /**
   * The thread to step: the one whose frame was shown last, else the main thread. Kept by name
   * across steps, since every step sends fresh snapshots.
   */
  get current(): PausedThread | undefined {
    return this.threads.find((thread) => thread.thread === this.focused) ?? this.threads[0];
  }

  focus(thread: PausedThread): void {
    this.focused = thread.thread;
  }

  add(thread: PausedThread): void {
    if (this.stale) {
      this.stale = false;
      this.threads = [];
    }
    this.generation++;
    this.threads = [...this.threads.filter((known) => known.id !== thread.id), thread];
    this.threads.sort((a, b) => Number(b.thread === "MainThread") - Number(a.thread === "MainThread"));
    this.changed.fire();
    this.decorate();
  }

  /** A step started: keep showing the old snapshots until the new ones come, without flashing empty. */
  markStale(): void {
    this.stale = this.threads.length > 0;
  }

  clear(): void {
    this.stale = false;
    if (this.threads.length) {
      this.threads = [];
      this.changed.fire();
      this.decorate();
    }
  }

  getTreeItem(item: Item): vscode.TreeItem {
    const { Collapsed, Expanded, None } = vscode.TreeItemCollapsibleState;
    const state = (byDefault: boolean) => ((this.expanded.get(item.key) ?? byDefault) ? Expanded : Collapsed);
    let treeItem: vscode.TreeItem;
    if (item.kind === "thread") {
      treeItem = new vscode.TreeItem(item.thread.thread, state(true));
      treeItem.description = "paused";
      treeItem.iconPath = new vscode.ThemeIcon("debug-pause");
    } else if (item.kind === "frame") {
      const { frame } = item;
      treeItem = new vscode.TreeItem(frame.name, state(item.top));
      treeItem.description = `${vscode.workspace.asRelativePath(frame.file)}:${frame.line}`;
      treeItem.iconPath = new vscode.ThemeIcon(item.top ? "debug-stackframe" : "debug-stackframe-dot");
      treeItem.tooltip = frame.node;
      treeItem.command = { command: "luciole.revealFrame", title: "Show", arguments: [frame, item.thread] };
    } else {
      const { variable } = item;
      treeItem = new vscode.TreeItem(variable.name, variable.ref ? state(false) : None);
      treeItem.description = variable.value;
      treeItem.tooltip = variable.type ? `${variable.type}: ${variable.value}` : variable.value;
    }
    treeItem.id = `${this.generation}|${item.key}`;
    return treeItem;
  }

  async getChildren(item?: Item): Promise<Item[]> {
    if (!item) {
      // Like the debugger: the threads only show when there are several.
      return this.threads.length === 1
        ? this.frames(this.threads[0])
        : this.threads.map((thread) => ({ kind: "thread", thread, key: `thread:${thread.thread}` }));
    }
    if (item.kind === "thread") {
      return this.frames(item.thread);
    }
    const vars = item.kind === "frame" ? item.frame.vars : item.variable.ref ? await this.expand(item.variable.ref) : [];
    return vars.map((variable) => ({ kind: "variable", variable, key: `${item.key}/${variable.name}` }));
  }

  dispose(): void {
    this.subscriptions.forEach((subscription) => subscription.dispose());
  }

  private frames(thread: PausedThread): Item[] {
    const depth = thread.frames.length;
    return thread.frames.map((frame, index) => ({
      kind: "frame",
      frame,
      thread,
      top: index === 0,
      key: `thread:${thread.thread}/${depth - index}:${frame.name}`,
    }));
  }

  /** Highlight the line where each thread stopped. */
  private decorate(): void {
    for (const editor of vscode.window.visibleTextEditors) {
      const lines = this.threads
        .map((thread) => thread.frames[0])
        .filter((frame) => frame && frame.file === editor.document.uri.fsPath)
        .map((frame) => new vscode.Range(frame.line - 1, 0, frame.line - 1, 0));
      editor.setDecorations(this.stoppedLine, lines);
    }
  }
}
