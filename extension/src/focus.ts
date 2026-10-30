/**
 * Focus and filters on the brain, from VS Code's side: "Focus Around" (a Map or Calls item, or a
 * function picked) shows only the functions a few calls around one, "Filters…" hides tests and
 * private helpers. The webview shades the rest (webview/filters.ts); this keeps the choice and
 * sends it to whichever panel shows a graph.
 */

import * as vscode from "vscode";
import type { HistoryItem } from "./calls";
import type { MapEntry } from "./map";
import type { ToWebview } from "./messages";
import { Filters, NO_FILTERS } from "./webview/filters";

export class BrainFilters {
  current: Filters = NO_FILTERS;

  constructor(
    private readonly post: (message: ToWebview) => void,
    private readonly functions: () => MapEntry[],
  ) {}

  get active(): boolean {
    return Boolean(this.current.around || this.current.hideTests || this.current.hidePrivate);
  }

  set(next: Filters): void {
    this.current = next;
    void vscode.commands.executeCommand("setContext", "luciole.filtered", this.active);
    this.post({ type: "filters", filters: next });
  }

  /** Only the functions within `luciole.focusDepth` calls of `target` (a Map or Calls item, else one picked) stand out. */
  async focusAround(target: MapEntry | HistoryItem | undefined): Promise<void> {
    let node = target && "target" in target && target.kind === "function" ? target.target : target && "call" in target ? target.call.node : undefined;
    if (!node) {
      const picked = await vscode.window.showQuickPick(
        this.functions().map((entry) => ({ label: `$(symbol-function) ${entry.label}`, description: entry.container, entry })),
        { title: "Luciole: Focus Around", placeHolder: "Show only the functions a few calls around this one", matchOnDescription: true },
      );
      node = picked?.entry.target;
    }
    if (node) {
      this.set({ ...this.current, around: node, depth: vscode.workspace.getConfiguration("luciole").get<number>("focusDepth", 1) });
    }
  }

  /** Hide tests, hide private helpers, keep or end the focus. */
  async pick(): Promise<void> {
    type Pick = vscode.QuickPickItem & { key: "hideTests" | "hidePrivate" | "around" };
    const filters = this.current;
    const items: Pick[] = [
      { key: "hideTests", label: "Hide tests", description: "test files, test_ functions, Test classes", picked: filters.hideTests },
      { key: "hidePrivate", label: "Hide private helpers", description: "functions named _like_this", picked: filters.hidePrivate },
    ];
    if (filters.around) {
      items.push({ key: "around", label: "Keep the focus", description: `only the functions around ${filters.around.split(":").pop()}`, picked: true });
    }
    const picked = await vscode.window.showQuickPick(items, { title: "Luciole: Filters", canPickMany: true, placeHolder: "What the brain leaves out (it stays in place, nearly invisible)" });
    if (picked) {
      const on = new Set(picked.map((item) => item.key));
      this.set({ ...filters, hideTests: on.has("hideTests"), hidePrivate: on.has("hidePrivate"), around: on.has("around") ? filters.around : undefined });
    }
  }
}
