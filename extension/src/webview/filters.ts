/**
 * Focus and filters on the brain: show only the functions a few calls around one (the others
 * dimmed), and hide tests and private helpers (`_name`). Nothing is removed or laid out again:
 * shaded neurons and lines are drawn fainter in the same batches, so a filter costs no draw call
 * and the map keeps its shape. Pure logic.
 */

import type { Brain, Neuron } from "./brain";

export interface Filters {
  /** Only the functions within `depth` calls of this one (either way) stand out. */
  around?: string;
  depth: number;
  hideTests: boolean;
  hidePrivate: boolean;
}

export const NO_FILTERS: Filters = { depth: 1, hideTests: false, hidePrivate: false };

/** How a neuron is drawn: dimmed (outside the focus) or nearly hidden (filtered out). */
export type Shade = "dim" | "hidden";

/** Test code: in a test file (`test_*.py`, `*_test.py`, `conftest.py`, a `tests` folder), or a `test_` function or `Test` class. */
export function isTest(neuron: Neuron): boolean {
  const file = neuron.node.file.replace(/\\/g, "/");
  const name = file.split("/").pop() ?? "";
  if (/^test_.*\.py$|_test\.py$|^conftest\.py$/.test(name) || /(^|\/)tests?\//.test(file)) {
    return true;
  }
  return neuron.label.split(".").some((part) => /^test_|^Test[A-Z_]/.test(part));
}

/** A private helper: its own name starts with `_` (not a `__dunder__` method; module code never). */
export function isPrivate(neuron: Neuron): boolean {
  if (neuron.node.kind === "module") {
    return false;
  }
  const own = neuron.label.split(".").pop() ?? "";
  return own.startsWith("_") && !(own.startsWith("__") && own.endsWith("__"));
}

/** Functions within `depth` calls of `id`, callers and callees alike (calls, runtime calls, spawns), itself included. */
export function neighborhood(brain: Brain, id: string, depth: number): Set<string> {
  const next = new Map<string, string[]>();
  for (const synapse of brain.synapses) {
    if (synapse.kind === "state") {
      continue;
    }
    next.set(synapse.source, [...(next.get(synapse.source) ?? []), synapse.target]);
    next.set(synapse.target, [...(next.get(synapse.target) ?? []), synapse.source]);
  }
  const seen = new Set([id]);
  let ring = [id];
  for (let step = 0; step < depth && ring.length; step++) {
    ring = ring.flatMap((from) => next.get(from) ?? []).filter((to) => !seen.has(to) && (seen.add(to), true));
  }
  return seen;
}

/** Each shaded neuron and how; unshaded ones are left out. */
export function shades(brain: Brain, filters: Filters): Map<string, Shade> {
  const result = new Map<string, Shade>();
  const near = filters.around && brain.neurons.some((n) => n.id === filters.around) ? neighborhood(brain, filters.around, filters.depth) : undefined;
  for (const neuron of brain.neurons) {
    if ((filters.hideTests && isTest(neuron)) || (filters.hidePrivate && isPrivate(neuron))) {
      result.set(neuron.id, "hidden");
    } else if (near && !near.has(neuron.id)) {
      result.set(neuron.id, "dim");
    }
  }
  return result;
}

/** What the filters do, for the status line: `around Cart.add (1 call) · tests hidden`. */
export function filtersText(filters: Filters, labelOf: (id: string) => string | undefined): string {
  const parts: string[] = [];
  if (filters.around) {
    parts.push(`around ${labelOf(filters.around) ?? filters.around} (${filters.depth} call${filters.depth === 1 ? "" : "s"})`);
  }
  if (filters.hideTests) {
    parts.push("tests hidden");
  }
  if (filters.hidePrivate) {
    parts.push("_private hidden");
  }
  return parts.join(" · ");
}
