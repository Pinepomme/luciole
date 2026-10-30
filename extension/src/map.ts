/**
 * The Map view's outline of the graph in the panel: packages, modules, classes and functions, the
 * same regions the brain is drawn with (buildRegions), so an item of the outline is a region or a
 * neuron of the brain. Pure logic (no VS Code).
 */

import type { Graph, GraphNode } from "./graph";
import { buildBrain } from "./webview/brain";
import { buildRegions } from "./webview/layout";

export type MapKind = "package" | "module" | "class" | "function";

export interface MapEntry {
  /** Unique in the tree: the region id, or `fn:` and the neuron id. */
  key: string;
  kind: MapKind;
  label: string;
  /** Region id (focusRegion) or neuron id (focus) in the brain. */
  target: string;
  parent?: string;
  children: string[];
  /** Functions inside, at any depth. */
  size: number;
  /** Functions: where the code is, relative to the graph's root. */
  file?: string;
  line?: number;
  col?: number;
  /** Functions: the module or class it is in, for search (`shop.Cart`). */
  container?: string;
}

export interface MapTree {
  roots: string[];
  entries: Map<string, MapEntry>;
}

const KIND_ORDER: Record<MapKind, number> = { package: 0, module: 1, class: 2, function: 3 };

export function buildMap(graph: Graph): MapTree {
  const brain = buildBrain(graph);
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  const { regions } = buildRegions(brain, nodes);
  const neurons = new Map(brain.neurons.map((neuron) => [neuron.id, neuron]));
  const entries = new Map<string, MapEntry>();
  const visit = (regionId: string, parent: string | undefined): string[] => {
    const region = regions.get(regionId)!;
    const keys: string[] = [];
    for (const id of region.regions) {
      const child = regions.get(id)!;
      const kind = child.kind as Exclude<MapKind, "function">;
      const entry: MapEntry = { key: id, kind, label: child.label, target: id, parent, children: [], size: child.size };
      entries.set(id, entry);
      entry.children = visit(id, id);
      keys.push(id);
    }
    // The class around the region's own functions, to name them inside it (`add`, not `Cart.add`).
    const cls = region.kind === "class" ? nodes.get(region.id.slice("cls:".length)) : undefined;
    for (const id of region.neurons) {
      const neuron = neurons.get(id)!;
      const { node } = neuron;
      const key = `fn:${id}`;
      entries.set(key, {
        key,
        kind: "function",
        label: functionLabel(node, neuron.label, cls),
        target: id,
        parent,
        children: [],
        size: 1,
        file: node.file,
        line: node.kind === "module" ? 1 : node.loc.line,
        col: node.kind === "module" ? 0 : node.loc.col,
        container: containerOf(id),
      });
      keys.push(key);
    }
    return sortEntries(keys, entries);
  };
  return { roots: visit("", undefined), entries };
}

/** The module, or class, a function is in: `shop.Cart` for `shop:Cart.add`. */
function containerOf(id: string): string {
  const colon = id.indexOf(":");
  if (colon < 0) {
    return id;
  }
  const qualified = id.slice(colon + 1);
  const dot = qualified.lastIndexOf(".");
  return dot < 0 ? id.slice(0, colon) : `${id.slice(0, colon)}.${qualified.slice(0, dot)}`;
}

/** A function's name inside its class (`add`, `outer.inner`), and `<module>` for module-level code. */
function functionLabel(node: GraphNode, label: string, cls: GraphNode | undefined): string {
  if (node.kind === "module") {
    return "<module>";
  }
  const prefix = cls ? `${cls.id.slice(cls.id.indexOf(":") + 1)}.` : "";
  return prefix && label.startsWith(prefix) ? label.slice(prefix.length) : label;
}

/** Packages, modules and classes by name, then functions in source order (module code first). */
function sortEntries(keys: string[], entries: Map<string, MapEntry>): string[] {
  return keys.sort((a, b) => {
    const ea = entries.get(a)!;
    const eb = entries.get(b)!;
    if (ea.kind !== eb.kind) {
      return KIND_ORDER[ea.kind] - KIND_ORDER[eb.kind];
    }
    if (ea.kind === "function") {
      return ea.file !== eb.file ? (ea.file! < eb.file! ? -1 : 1) : ea.line! - eb.line! || ea.label.localeCompare(eb.label);
    }
    return ea.label.localeCompare(eb.label);
  });
}

/** How many functions running inside each entry, from the neurons on the live stacks. */
export function runningCounts(tree: MapTree, running: Iterable<string>): Map<string, number> {
  const counts = new Map<string, number>();
  for (const id of running) {
    for (let entry = tree.entries.get(`fn:${id}`); entry; entry = entry.parent !== undefined ? tree.entries.get(entry.parent) : undefined) {
      counts.set(entry.key, (counts.get(entry.key) ?? 0) + 1);
    }
  }
  return counts;
}

/** The grey text after an item's name: its size, and what runs inside. */
export function mapDescription(entry: MapEntry, running: number): string {
  if (entry.kind === "function") {
    return running ? "running" : "";
  }
  const size = `${entry.size} function${entry.size === 1 ? "" : "s"}`;
  return running ? `${size} · ${running} running` : size;
}

/** Every function of the tree, in tree order, for a quick pick. */
export function mapFunctions(tree: MapTree): MapEntry[] {
  const out: MapEntry[] = [];
  const walk = (keys: string[]) => {
    for (const key of keys) {
      const entry = tree.entries.get(key)!;
      if (entry.kind === "function") {
        out.push(entry);
      }
      walk(entry.children);
    }
  };
  walk(tree.roots);
  return out;
}
