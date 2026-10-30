/**
 * Text shown with the brain: hover tooltips (text only: names come from the analyzed code) and
 * the bottom bar's lines in live mode.
 */

import type { SourceLine, ThreadStack } from "../messages";
import { breakpointAction, breakpointHint, Neuron, Synapse } from "./brain";

/** Lines of the bottom bar: one per thread, then what is waiting. */
const MAX_PATH_LINES = 5;

function escape(text: string): string {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
}

/** `neurons` and `breakpoints` say what a right-click on it would do. */
export function neuronTooltip(neuron: Neuron, neurons: Neuron[], breakpoints: SourceLine[]): string {
  const name = neuron.node.kind === "module" ? `module ${neuron.label}` : `${neuron.label}(${neuron.params.join(", ")})`;
  const action = breakpointAction(neurons, neuron, breakpoints);
  const hint = action ? `<br><span class="dim">right-click: ${escape(breakpointHint(neuron, action))}</span>` : "";
  return `<b>${escape(name)}</b><br><span class="dim">${escape(neuron.node.file)}:${neuron.node.loc.line}</span>${hint}`;
}

export function synapseTooltip(synapse: Synapse): string {
  const callTitle = synapse.byName
    ? "probably calls (matched by method name)"
    : synapse.callback
      ? "passes it to be called later (callback, thread, task)"
      : "calls";
  const title = { call: callTitle, state: "shared state", runtime: "call seen at runtime", spawn: "starts a thread or task" }[synapse.kind];
  const variables = synapse.variables.map((v) => escape(v)).join("<br>");
  return `<span class="dim">${title}</span>${variables ? `<br>${variables}` : ""}`;
}

/**
 * One line per thread with what it runs, then what is waiting on an await. `labelOf` names a
 * function of the brain (undefined for the others: their waits are left out).
 */
export function pathText(threads: ThreadStack[], waitingCounts: Record<string, number>, labelOf: (id: string) => string | undefined): string {
  const name = (id: string) => labelOf(id)!;
  const lines = threads.map(({ thread, stack }) => {
    const path = stack.map(name).join("  →  ");
    return threads.length > 1 || thread !== "MainThread" ? `${thread}:  ${path}` : path;
  });
  const waits = Object.entries(waitingCounts)
    .filter(([id]) => labelOf(id) !== undefined)
    .map(([id, count]) => (count > 1 ? `${name(id)} ×${count}` : name(id)));
  if (waits.length) {
    lines.push(`waiting:  ${waits.join(", ")}`);
  }
  if (lines.length > MAX_PATH_LINES) {
    lines.splice(MAX_PATH_LINES - 1, lines.length, `… and ${lines.length - MAX_PATH_LINES + 1} more`);
  }
  return lines.join("\n");
}
