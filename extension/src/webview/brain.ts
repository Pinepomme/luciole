/**
 * The 3D "brain" view of a graph: one neuron per function, synapses between them. Pure logic.
 *
 * A synapse is the set of variables flowing between two functions:
 * - `call` synapses follow calls, caller to callee. They carry the arguments passed
 *   (`subtotal → amount`) and the values returned (`due ← return`).
 * - `state` synapses go from a function that writes a global or an attribute to each
 *   function that reads it, and carry that variable's name.
 * - `runtime` synapses are calls seen while the program ran that static analysis missed.
 * - `spawn` synapses, also from live runs, go from where a thread or asyncio task was started
 *   to the function it runs.
 * Module-level code is a neuron too, when it calls something or writes globals.
 */

import type { Graph, GraphNode } from "../graph";
import type { SourceLine } from "../messages";

export type SynapseKind = "call" | "state" | "runtime" | "spawn";

export interface Neuron {
  id: string;
  node: GraphNode;
  /** Name inside its module: `Cart.add`, or the module name for module-level code. */
  label: string;
  /** Id of the module the function is in. */
  module: string;
  /** Parameter names, for display (without `self`). */
  params: string[];
  /** Number of synapses touching it. */
  degree: number;
}

export interface Synapse {
  id: string;
  kind: SynapseKind;
  source: string;
  target: string;
  variables: string[];
  /** Number of call sites (calls) or of variables (state). */
  count: number;
  /** Calls the analyzer matched by method name only: likely, not certain. */
  byName?: boolean;
  /** Calls to a function passed as an argument (`Thread(target=f)`), made later by what receives it. */
  callback?: boolean;
}

export interface Brain {
  neurons: Neuron[];
  synapses: Synapse[];
}

/** A variable shared by more writer × reader pairs than this is too generic to draw. */
export const MAX_STATE_PAIRS = 64;

/**
 * The innermost neuron whose code holds `line` of `file` (a path relative to the graph's root):
 * a function, else its module. Where the debugger stopped, or where a breakpoint is.
 */
export function neuronAt(neurons: Neuron[], file: string, line: number): Neuron | undefined {
  const span = (neuron: Neuron) => (neuron.node.kind === "module" ? Infinity : neuron.node.loc.end_line - neuron.node.loc.line);
  let best: Neuron | undefined;
  for (const neuron of neurons) {
    const { node } = neuron;
    const inside = node.kind === "module" || (node.loc.line <= line && line <= node.loc.end_line);
    if (node.file === file && inside && (!best || span(neuron) < span(best))) {
      best = neuron;
    }
  }
  return best;
}

/** What a right-click on a function's neuron does to its breakpoints. */
export type BreakpointAction = { add: number } | { remove: number[] };

/**
 * A right-click on a function: removes every breakpoint that marks it (any line of its code, as
 * for the red dot: see neuronAt), else adds one where each call starts (`body_line`: not the `def`
 * line, which runs once, at import). Undefined for module code.
 */
export function breakpointAction(neurons: Neuron[], neuron: Neuron, breakpoints: SourceLine[]): BreakpointAction | undefined {
  const { node } = neuron;
  if (node.kind !== "function") {
    return undefined;
  }
  const lines = breakpoints.filter(({ file, line }) => file === node.file && neuronAt(neurons, file, line)?.id === neuron.id).map(({ line }) => line);
  const unique = [...new Set(lines)].sort((a, b) => a - b);
  return unique.length ? { remove: unique } : { add: node.body_line ?? node.loc.line };
}

/** The right-click hint for `action` on `neuron`. */
export function breakpointHint(neuron: Neuron, action: BreakpointAction): string {
  if ("add" in action) {
    return "stop on each call";
  }
  const lines = action.remove;
  if (lines.length > 1) {
    return `remove its ${lines.length} breakpoints (lines ${lines.join(", ")})`;
  }
  return lines[0] === (neuron.node.body_line ?? neuron.node.loc.line) ? "remove the breakpoint" : `remove the breakpoint (line ${lines[0]})`;
}

export function synapseId(kind: SynapseKind, source: string, target: string): string {
  return `${kind}|${source}>${target}`;
}

export function buildBrain(graph: Graph): Brain {
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]));
  const parentOf = (node: GraphNode) => (node.parent ? nodes.get(node.parent) : undefined);
  const synapses = new Map<string, Synapse>();
  const link = (kind: SynapseKind, source: string, target: string, variable?: string, count = 1) => {
    const id = synapseId(kind, source, target);
    let synapse = synapses.get(id);
    if (!synapse) {
      synapse = { id, kind, source, target, variables: [], count: 0 };
      synapses.set(id, synapse);
    }
    synapse.count += count;
    if (variable && !synapse.variables.includes(variable)) {
      synapse.variables.push(variable);
    }
  };

  const readers = new Map<string, Set<string>>();
  const writers = new Map<string, Set<string>>();
  for (const edge of graph.edges) {
    if (edge.kind === "reads") {
      addTo(readers, edge.source, edge.target);
    } else if (edge.kind === "writes") {
      addTo(writers, edge.target, edge.source);
    }
  }

  for (const edge of graph.edges) {
    // Calls to functions, and imports, which run the imported module.
    const target = nodes.get(edge.target)?.kind;
    if (edge.kind === "calls" && (target === "function" || target === "module")) {
      link("call", edge.source, edge.target, undefined, edge.sites?.length ?? 1);
      const synapse = synapses.get(synapseId("call", edge.source, edge.target))!;
      synapse.byName = edge.by_name || undefined;
      synapse.callback = edge.callback || undefined;
    }
  }

  /** The function a variable is local to; undefined for globals and attributes. */
  const localTo = (variable: GraphNode) => {
    const parent = parentOf(variable);
    return parent?.kind === "function" ? parent.id : undefined;
  };
  /** How a value source reads in a label: its name, or `f()` for a function's return value. */
  const valueName = (variable: GraphNode) =>
    variable.role === "return" ? `${parentOf(variable)?.name ?? "?"}()` : variable.name;

  for (const edge of graph.edges) {
    const source = nodes.get(edge.source);
    const target = nodes.get(edge.target);
    if (!source || !target) {
      continue;
    }
    if (edge.kind === "passes_arg") {
      const callee = localTo(target);
      if (!callee) {
        continue;
      }
      // The caller is the function the value is local to; for a global or an attribute,
      // any function that reads it and calls the callee.
      const local = localTo(source);
      const callers = local ? [local] : [...(readers.get(source.id) ?? [])].filter((f) => synapses.has(synapseId("call", f, callee)));
      const name = valueName(source);
      for (const caller of callers) {
        link("call", caller, callee, name === target.name ? name : `${name} → ${target.name}`, 0);
      }
    } else if (edge.kind === "returns") {
      const callee = localTo(source);
      const caller = localTo(target);
      if (callee && caller) {
        link("call", caller, callee, target.role === "return" ? "return ← return" : `${target.name} ← return`, 0);
      }
    }
  }

  for (const variable of graph.nodes) {
    if (variable.role !== "global" && variable.role !== "class_attr" && variable.role !== "instance_attr") {
      continue;
    }
    const from = [...(writers.get(variable.id) ?? [])];
    const to = [...(readers.get(variable.id) ?? [])];
    if (from.length * to.length > MAX_STATE_PAIRS) {
      continue;
    }
    for (const writer of from) {
      for (const reader of to) {
        if (writer !== reader) {
          link("state", writer, reader, variable.name);
        }
      }
    }
  }

  const degree = new Map<string, number>();
  for (const synapse of synapses.values()) {
    degree.set(synapse.source, (degree.get(synapse.source) ?? 0) + 1);
    degree.set(synapse.target, (degree.get(synapse.target) ?? 0) + 1);
  }
  const params = new Map<string, GraphNode[]>();
  for (const node of graph.nodes) {
    if (node.role === "param" && node.parent) {
      params.set(node.parent, [...(params.get(node.parent) ?? []), node]);
    }
  }
  const neurons: Neuron[] = [];
  for (const node of graph.nodes) {
    if (node.kind === "function" || (node.kind === "module" && degree.has(node.id))) {
      neurons.push(makeNeuron(node, nodes, params.get(node.id) ?? [], degree.get(node.id) ?? 0));
    }
  }
  const neuronIds = new Set(neurons.map((n) => n.id));
  const kept = [...synapses.values()].filter((s) => neuronIds.has(s.source) && neuronIds.has(s.target));
  return { neurons, synapses: kept };
}

function makeNeuron(node: GraphNode, nodes: Map<string, GraphNode>, params: GraphNode[], degree: number): Neuron {
  let module = node;
  while (module.kind !== "module" && module.parent && nodes.has(module.parent)) {
    module = nodes.get(module.parent)!;
  }
  const ordered = [...params].sort((a, b) => a.param_index! - b.param_index!);
  const isMethod = nodes.get(node.parent ?? "")?.kind === "class" && !node.decorators?.includes("staticmethod");
  return {
    id: node.id,
    node,
    label: node.kind === "module" ? node.name : node.id.slice(node.id.indexOf(":") + 1),
    module: module.id,
    params: (isMethod ? ordered.slice(1) : ordered).map((p) => p.name),
    degree,
  };
}

function addTo(map: Map<string, Set<string>>, key: string, value: string): void {
  const set = map.get(key);
  if (set) {
    set.add(value);
  } else {
    map.set(key, new Set([value]));
  }
}
