/**
 * "Where did this value come from?" from VS Code: asked from the Values panel, from a value in
 * the Calls view, or from the editor (the name under the cursor, in the latest recorded call of
 * its function), answered by the trace's query server (`origin` in src/luciole/browse.py). The
 * answer shows in the Origin view, a tree of steps (origin.ts), and lights its path on the brain;
 * a step clicked opens its line and pulses its function. A live run or a replay starting clears
 * it (the brain shows the run then), as does Clear Origin.
 */

import * as path from "path";
import * as vscode from "vscode";
import { HistoryItem, RecordedRun, RunListing, runTime, shortNode, TraceCall } from "./calls";
import { callChoice, callValues, identifierAt, OriginAnswer, OriginItem, originItems, OriginPath, originPath, OriginTarget, originTitle, runTarget } from "./origin";
import type { RunConfiguration } from "./runs";
import type { Traces } from "./traces";

export interface OriginHooks {
  traces: Traces;
  /** The recorded runs of the workspace (the Calls view's). */
  runs(): RunListing[];
  /** Light `path` on the brain of `root` (showing its graph first if needed), or clear it (undefined). */
  light(root: string, path: OriginPath | undefined): void;
  pulse(node: string): void;
  /** Run `config` again with Run Live, recording `scope` at level 2. */
  rerun(config: RunConfiguration, scope: string): Promise<void>;
  log(line: string): void;
}

/** Runs looked through, newest first, for a call of the function under the cursor. */
const RUNS_SEARCHED = 20;

export class OriginView implements vscode.TreeDataProvider<OriginItem>, vscode.Disposable {
  private readonly changed = new vscode.EventEmitter<OriginItem | undefined>();
  readonly onDidChangeTreeData = this.changed.event;
  readonly view: vscode.TreeView<OriginItem>;
  private shown: { target: OriginTarget; answer: OriginAnswer; items: OriginItem[]; path: OriginPath } | undefined;

  constructor(private readonly hooks: OriginHooks) {
    this.view = vscode.window.createTreeView("luciole.origin", { treeDataProvider: this, showCollapseAll: true });
  }

  dispose(): void {
    this.view.dispose();
    this.changed.dispose();
  }

  /** Where From? (on a Calls item, else in the editor), Clear Origin, and the items' own commands. */
  commands(): vscode.Disposable[] {
    return [
      vscode.commands.registerCommand("luciole.whereFrom", (item?: HistoryItem) => (item && "kind" in item ? this.askAbout(item) : this.askAtCursor())),
      vscode.commands.registerCommand("luciole.clearOrigin", () => this.clear()),
      vscode.commands.registerCommand("luciole.originStep", (item: OriginItem) => this.showStep(item)),
      vscode.commands.registerCommand("luciole.originContinue", (item: OriginItem) => this.goOn(item)),
      vscode.commands.registerCommand("luciole.originRerun", (item: OriginItem) => this.rerun(item)),
    ];
  }

  /** The path lit on the brain now, to light again when its graph is shown again. */
  get path(): OriginPath | undefined {
    return this.shown?.path;
  }

  // Asking

  /** Asks where `target`'s value came from, and shows the answer. */
  async ask(target: OriginTarget): Promise<void> {
    let answer: OriginAnswer;
    try {
      answer = await vscode.window.withProgress(
        { location: { viewId: "luciole.calls" }, title: "Luciole: where did it come from?" },
        () => this.hooks.traces.query<OriginAnswer>(target.file, "origin", { node: target.node, variable: target.variable, frame: target.frame, ...(target.at !== undefined ? { at: target.at } : {}) }),
      );
    } catch (error) {
      void vscode.window.showErrorMessage(`Luciole: ${error instanceof Error ? error.message : error}`);
      return;
    }
    const items = originItems(answer);
    const path = originPath(answer);
    this.shown = { target, answer, items, path };
    this.hooks.log(answer.text);
    await vscode.commands.executeCommand("setContext", "luciole.hasOrigin", true);
    this.view.title = "Origin";
    this.view.description = originTitle(answer);
    this.view.message = undefined;
    this.changed.fire(undefined);
    this.hooks.light(answer.root, path);
    await vscode.commands.executeCommand("luciole.origin.focus");
  }

  /** A value of the Calls view, or a call (or traceback frame): pick which of its values. */
  async askAbout(item: HistoryItem): Promise<void> {
    if (item.kind === "value" && item.origin) {
      return this.ask(item.origin);
    }
    if (item.kind !== "call" && item.kind !== "where") {
      return;
    }
    const call = (await this.hooks.traces.query<TraceCall | null>(item.file, "frame", { frame: item.call.frame }).catch(() => null)) ?? item.call;
    const values = callValues(call);
    if (!values.length) {
      void vscode.window.showInformationMessage(`Luciole: no recorded values for ${shortNode(call.node)} #${call.frame} (recorded at level 0).`);
      return;
    }
    const picked = await vscode.window.showQuickPick(
      values.map((value) => ({ label: value.variable === "return" ? "$(debug-step-out) return" : `$(symbol-variable) ${value.variable}`, description: value.value?.repr ?? "", value })),
      { title: `Luciole: where did it come from? (${shortNode(call.node)} #${call.frame})`, placeHolder: "Which value" },
    );
    if (picked) {
      await this.ask({ file: item.file, root: item.root, frame: call.frame, node: call.node, variable: picked.value.variable, at: picked.value.at });
    }
  }

  /**
   * The editor: the name under the cursor (or selected), in the function holding the cursor, in
   * the latest recorded run that called that function: its last call, or one picked.
   */
  async askAtCursor(): Promise<void> {
    const editor = vscode.window.activeTextEditor;
    const document = editor?.document;
    if (!editor || document?.languageId !== "python" || document.uri.scheme !== "file") {
      void vscode.window.showErrorMessage("Luciole: put the cursor on a variable in a Python file.");
      return;
    }
    const position = editor.selection.active;
    const selected = editor.selection.isEmpty || !editor.selection.isSingleLine ? undefined : document.getText(editor.selection).trim();
    const variable = selected && /^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$/.test(selected) ? selected : identifierAt(document.lineAt(position.line).text, position.character);
    if (!variable) {
      void vscode.window.showErrorMessage("Luciole: put the cursor on a variable (a local, a parameter, a global, self.x) or on return.");
      return;
    }
    const file = document.uri.fsPath;
    const line = position.line + 1;
    const found = await vscode.window.withProgress({ location: vscode.ProgressLocation.Window, title: "Luciole: finding the last call" }, () => this.lastCalls(file, line));
    if ("problem" in found) {
      void vscode.window.showInformationMessage(`Luciole: ${found.problem}`);
      return;
    }
    let call = found.calls[0];
    if (found.calls.length > 1) {
      const when = runTime(found.run.started_at, new Date());
      const picked = await vscode.window.showQuickPick(
        found.calls.map((each, i) => ({ ...callChoice(each, i === 0 ? `${when}, the last call` : when), call: each })),
        { title: `Luciole: where did ${variable} come from? ${found.total} calls of ${shortNode(found.node)}`, placeHolder: "Which call (the newest first)" },
      );
      if (!picked) {
        return;
      }
      call = picked.call;
    }
    await this.ask({ file: found.trace, root: found.root, frame: call.frame, node: found.node, variable });
  }

  /** The function at `file`:`line` and its last calls, in the newest run (of its workspace folder) that called it. */
  private async lastCalls(file: string, line: number): Promise<{ problem: string } | { root: string; run: RecordedRun; trace: string; node: string; total: number; calls: TraceCall[] }> {
    const listings = this.hooks.runs().filter((listing) => !path.relative(listing.root, file).startsWith(".."));
    const runs = listings
      .flatMap((listing) => listing.runs.filter((run) => run.processes.some((p) => p.frames > 0)).map((run) => ({ run, root: listing.root })))
      .sort((a, b) => b.run.started_at - a.run.started_at)
      .slice(0, RUNS_SEARCHED);
    if (!runs.length) {
      return { problem: "no recorded run of this folder yet. Run Live records each run; then ask again." };
    }
    let node: string | undefined;
    for (const { run, root } of runs) {
      for (const process of run.processes.filter((p) => p.frames > 0)) {
        try {
          node ??= (await this.hooks.traces.query<{ node: string } | null>(process.file, "function_at", { file, line }))?.node;
          if (!node) {
            return { problem: "this line is not in the recorded project's code." };
          }
          const calls = await this.hooks.traces.query<{ total: number; calls: TraceCall[] }>(process.file, "last_calls", { node, limit: 50 });
          if (calls.total) {
            return { root, run, trace: process.file, node, total: calls.total, calls: calls.calls };
          }
        } catch (error) {
          this.hooks.log(`could not read ${process.file}: ${error}`);
        }
      }
    }
    return { problem: node ? `${shortNode(node)} did not run in the last ${runs.length} recorded runs.` : "could not read the recorded runs (see the Luciole output)." };
  }

  // The answer

  getTreeItem(item: OriginItem): vscode.TreeItem {
    const tree = new vscode.TreeItem(item.label, item.children.length ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.None);
    tree.id = item.key;
    tree.description = item.description;
    tree.tooltip = item.tooltip ?? (item.description ? `${item.label}\n${item.description}` : item.label);
    tree.contextValue = `origin.${item.kind}`;
    if (item.icon) {
      tree.iconPath = new vscode.ThemeIcon(item.icon);
    }
    if (item.kind === "rerun") {
      tree.command = { command: "luciole.originRerun", title: "Record and Run Again", arguments: [item] };
    } else if (item.kind === "continue") {
      tree.command = { command: "luciole.originContinue", title: "Go On From Here", arguments: [item] };
    } else if (item.at) {
      tree.command = { command: "luciole.originStep", title: "Show Step", arguments: [item] };
    }
    return tree;
  }

  getChildren(item?: OriginItem): OriginItem[] {
    return item ? item.children : (this.shown?.items ?? []);
  }

  /** A step: its line in the editor, its function pulsing on the brain. */
  showStep(item: OriginItem): void {
    const shown = this.shown;
    if (!shown || !item.at) {
      return;
    }
    this.hooks.pulse(item.at.node);
    const relative = shown.answer.files[item.at.node];
    if (!relative) {
      return;
    }
    const line = item.at.line ?? 1;
    const position = new vscode.Position(Math.max(line - 1, 0), 0);
    void vscode.window.showTextDocument(vscode.Uri.file(path.resolve(shown.answer.root, relative)), {
      viewColumn: vscode.ViewColumn.One,
      preserveFocus: true,
      selection: new vscode.Range(position, position),
    });
  }

  /** Where the walk stopped: ask again from that step. */
  async goOn(item: OriginItem): Promise<void> {
    const shown = this.shown;
    if (shown && item.question) {
      await this.ask({ ...shown.target, frame: item.question.frame, node: "", variable: item.question.variable, at: item.question.at });
    }
  }

  /** needs_level_2: Run Live of the same command, recording that function line by line. */
  async rerun(item: OriginItem): Promise<void> {
    const shown = this.shown;
    if (!shown || !item.scope) {
      return;
    }
    const run = this.hooks.runs().flatMap((listing) => listing.runs).find((each) => each.processes.some((p) => p.file === shown.target.file));
    const target = run && runTarget(run.command);
    const command = `luciole run --root ${shown.target.root} --scope ${item.scope} --trace DIR ${run ? run.command.slice(1).join(" ") : "…"}`;
    if (!run || !target) {
      await vscode.env.clipboard.writeText(command);
      void vscode.window.showInformationMessage(`Luciole: the run's command is not known; the command to record it is copied: ${command}`);
      return;
    }
    const config: RunConfiguration = { name: `${run.command.slice(1).join(" ")} (${shortNode(item.scope)} line by line)`, ...target, cwd: run.cwd, folder: shown.target.root };
    void vscode.window.showInformationMessage(`Luciole: running it again with ${shortNode(item.scope)}'s variables recorded (level 2). When it ends, ask again.`);
    await this.hooks.rerun(config, item.scope);
  }

  /** Clears the answer and the brain's path; `fromBrain`: the brain already cleared it (a run or a replay started). */
  clear(fromBrain = false): void {
    const shown = this.shown;
    if (!shown) {
      return;
    }
    this.shown = undefined;
    void vscode.commands.executeCommand("setContext", "luciole.hasOrigin", false);
    this.view.description = undefined;
    this.changed.fire(undefined);
    if (!fromBrain) {
      this.hooks.light(shown.answer.root, undefined);
    }
  }
}
