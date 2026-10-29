/**
 * Where neurons go: a map of regions inside regions. Pure logic (no three.js, no VS Code).
 *
 * Packages (directories) hold modules, modules hold classes, classes hold methods. Each region is
 * packed on its own, bottom-up: its items (child regions and its own neurons) are spheres pulled
 * together by how much they call each other and kept from overlapping. Then the regions are put
 * in place top-down, so nearness means "same package, same module, same class".
 *
 * The packing starts from positions seeded by a hash of each item's name and runs a fixed number
 * of steps, so the same project always gives the same map, and editing one file only moves
 * things inside its own region and a little around it.
 *
 * Calls between regions are drawn as bundles (`route`, `bundle`): a curve from the caller up
 * through the centres of its enclosing regions and down through the callee's, so calls between
 * two regions travel together like nerve tracts.
 */

import type { GraphNode } from "../graph";
import type { Brain, Neuron } from "./brain";

export interface Vec3 {
  x: number;
  y: number;
  z: number;
}

export type RegionKind = "root" | "package" | "module" | "class";

export interface Region {
  /** `pkg:a/b`, `mod:a.b.c`, `cls:a.b.c:Class`, or "" for the root. */
  id: string;
  kind: RegionKind;
  label: string;
  /** 0 for the root, 1 for top-level regions. */
  depth: number;
  parent?: string;
  /** Child regions and neurons directly inside, sorted by id. */
  regions: string[];
  neurons: string[];
  /** Neurons inside, at any depth. */
  size: number;
  center: Vec3;
  radius: number;
}

export interface Layout {
  regions: Map<string, Region>;
  /** Region ids from the root to the innermost region holding each neuron. */
  chains: Map<string, string[]>;
  positions: Map<string, Vec3>;
}

/** Room between two neurons of the same region, by default. */
export const NEURON_GAP = 9;
/** Room between sibling regions, plus a share of their size. */
const REGION_GAP = 8;
const STEPS = 160;
const GRAVITY = 0.12;

export function neuronRadius(degree: number): number {
  return 2.5 + Math.sqrt(degree) * 1.5;
}

/** FNV-1a: a stable 32-bit hash of a string. */
export function hash(text: string, seed = 0x811c9dc5): number {
  let h = seed >>> 0;
  for (let i = 0; i < text.length; i++) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** A point in the unit ball, the same for the same name. */
export function seedPoint(name: string): Vec3 {
  const u = hash(name) / 2 ** 32;
  const v = hash(name, 0x9e3779b9) / 2 ** 32;
  const w = hash(name, 0x85ebca6b) / 2 ** 32;
  const z = 2 * u - 1;
  const angle = 2 * Math.PI * v;
  const ring = Math.sqrt(1 - z * z);
  const radius = 0.35 + 0.65 * Math.cbrt(w);
  return { x: ring * Math.cos(angle) * radius, y: ring * Math.sin(angle) * radius, z: z * radius };
}

/**
 * The regions of a brain. Packages come from the directories of the files; a region with no
 * neuron of its own and a single child region is merged with it (`src/pkg`), so chains of
 * directories don't nest for nothing.
 */
export function buildRegions(brain: Brain, nodes: Map<string, GraphNode>): Pick<Layout, "regions" | "chains"> {
  const regions = new Map<string, Region>();
  const make = (id: string, kind: RegionKind, label: string, parent?: Region): Region => {
    let region = regions.get(id);
    if (!region) {
      region = { id, kind, label, depth: parent ? parent.depth + 1 : 0, parent: parent?.id, regions: [], neurons: [], size: 0, center: origin(), radius: 0 };
      regions.set(id, region);
      parent?.regions.push(id);
    }
    return region;
  };
  const root = make("", "root", "");
  const chains = new Map<string, string[]>();
  for (const neuron of brain.neurons) {
    const module = nodes.get(neuron.module);
    const file = module?.file ?? neuron.node.file;
    let region = root;
    const dirs = file.split(/[\\/]/).slice(0, -1);
    dirs.forEach((dir, i) => (region = make(`pkg:${dirs.slice(0, i + 1).join("/")}`, "package", dir, region)));
    const moduleName = module?.name ?? neuron.module;
    region = make(`mod:${neuron.module}`, "module", moduleName.slice(moduleName.lastIndexOf(".") + 1), region);
    for (const cls of classesAround(neuron, nodes)) {
      region = make(`cls:${cls.id}`, "class", cls.name, region);
    }
    region.neurons.push(neuron.id);
    const chain: string[] = [];
    for (let r: Region | undefined = region; r; r = r.parent !== undefined ? regions.get(r.parent) : undefined) {
      chain.unshift(r.id);
      r.size++;
    }
    chains.set(neuron.id, chain);
  }
  // Merge regions that only wrap another one.
  for (const region of [...regions.values()]) {
    if (region.kind === "root" || region.neurons.length || region.regions.length !== 1) {
      continue;
    }
    const child = regions.get(region.regions[0])!;
    const parent = regions.get(region.parent!)!;
    child.label = `${region.label}${region.kind === "package" ? "/" : "."}${child.label}`;
    child.kind = region.kind === "module" ? "module" : child.kind;
    child.parent = parent.id;
    parent.regions[parent.regions.indexOf(region.id)] = child.id;
    regions.delete(region.id);
    for (const chain of chains.values()) {
      const at = chain.indexOf(region.id);
      if (at >= 0) {
        chain.splice(at, 1);
      }
    }
  }
  const setDepth = (region: Region, depth: number) => {
    region.depth = depth;
    region.regions.sort();
    region.neurons.sort();
    region.regions.forEach((id) => setDepth(regions.get(id)!, depth + 1));
  };
  setDepth(root, 0);
  return { regions, chains };
}

/** Classes around a function, outermost first (methods of nested classes are in both). */
function classesAround(neuron: Neuron, nodes: Map<string, GraphNode>): GraphNode[] {
  const classes: GraphNode[] = [];
  for (let node = nodes.get(neuron.node.parent ?? ""); node && node.kind !== "module"; node = nodes.get(node.parent ?? "")) {
    if (node.kind === "class") {
      classes.unshift(node);
    }
  }
  return classes;
}

/**
 * The map: regions packed bottom-up, then placed top-down. `neuronGap` is the room between
 * neurons: more when their names are always shown.
 */
export function layoutBrain(brain: Brain, nodes: Map<string, GraphNode>, neuronGap = NEURON_GAP): Layout {
  const { regions, chains } = buildRegions(brain, nodes);
  const neurons = new Map(brain.neurons.map((n) => [n.id, n]));
  // Affinity between the two items of a region that a synapse joins (one entry per region).
  const affinity = new Map<string, Map<string, number>>();
  for (const synapse of brain.synapses) {
    const ends = items(chains, synapse.source, synapse.target);
    if (!ends || ends.a === ends.b) {
      continue;
    }
    const key = ends.a < ends.b ? `${ends.a}\n${ends.b}` : `${ends.b}\n${ends.a}`;
    const weights = affinity.get(ends.region) ?? new Map<string, number>();
    weights.set(key, (weights.get(key) ?? 0) + (synapse.kind === "state" ? 0.5 : 1));
    affinity.set(ends.region, weights);
  }
  const local = new Map<string, Vec3>(); // item position relative to its region's centre
  const pack = (region: Region) => {
    region.regions.forEach((id) => pack(regions.get(id)!));
    const packed: Item[] = [
      ...region.regions.map((id) => ({ key: id, radius: regions.get(id)!.radius, region: true })),
      ...region.neurons.map((id) => ({ key: `n:${id}`, radius: neuronRadius(neurons.get(id)!.degree), region: false })),
    ];
    // Sorted: the result must not depend on the order of the graph's edges.
    const links = [...(affinity.get(region.id) ?? [])].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)).map(([key, weight]) => {
      const [a, b] = key.split("\n");
      return { a, b, weight };
    });
    // Round at every level: the map is a 3D brain, not a plane (discs read flat once you turn it).
    const positions = packItems(packed, links, 1, neuronGap);
    packed.forEach((item, i) => local.set(item.key, positions[i]));
    region.radius = packed.reduce((r, item, i) => Math.max(r, length(positions[i]) + item.radius), 0) + (region.kind === "root" ? 0 : 3);
  };
  const root = regions.get("")!;
  pack(root);
  const positions = new Map<string, Vec3>();
  const place = (region: Region) => {
    for (const id of region.regions) {
      const child = regions.get(id)!;
      child.center = add(region.center, local.get(id)!);
      place(child);
    }
    for (const id of region.neurons) {
      positions.set(id, add(region.center, local.get(`n:${id}`)!));
    }
  };
  place(root);
  return { regions, chains, positions };
}

/**
 * Where a synapse sits in the hierarchy: the innermost region holding both ends, and the item
 * of that region each end is in (a child region, or `n:<id>` for a neuron directly inside).
 */
function items(chains: Map<string, string[]>, source: string, target: string): { region: string; a: string; b: string } | undefined {
  const ca = chains.get(source);
  const cb = chains.get(target);
  if (!ca || !cb) {
    return undefined;
  }
  const common = commonLength(ca, cb);
  return { region: ca[common - 1], a: ca[common] ?? `n:${source}`, b: cb[common] ?? `n:${target}` };
}

function commonLength(a: string[], b: string[]): number {
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) {
    i++;
  }
  return i;
}

interface Item {
  key: string;
  radius: number;
  region: boolean;
}

/**
 * Packs spheres around the origin: links pull items together, gravity keeps the pack round (or
 * a disc as thick as `flat` times its width), collisions keep them apart. Deterministic: starts
 * from seeded points, fixed steps.
 */
export function packItems(items: Item[], links: { a: string; b: string; weight: number }[], flat = 1, neuronGap = NEURON_GAP): Vec3[] {
  const n = items.length;
  if (n <= 1) {
    return items.map(origin);
  }
  const index = new Map(items.map((item, i) => [item.key, i]));
  const gap = (i: number, j: number) =>
    items[i].region || items[j].region ? REGION_GAP + (flat < 1 ? 0.12 : 0.05) * (items[i].radius + items[j].radius) : neuronGap;
  const volume = items.reduce((v, item) => v + (item.radius + gap(0, 0) / 2) ** 3, 0);
  const spread = Math.cbrt(volume / 0.3);
  const p = items.map((item) => {
    const point = scale(seedPoint(item.key), spread);
    point.z *= flat;
    return point;
  });
  const mass = items.map((item) => item.radius ** 3);
  const pairs = links.flatMap(({ a, b, weight }) => {
    const i = index.get(a);
    const j = index.get(b);
    return i === undefined || j === undefined ? [] : [{ i, j, strength: Math.min(1, 0.25 + 0.25 * Math.log2(weight)) }];
  });
  // Fewer steps for huge regions: collisions cost n² per step.
  const steps = Math.max(30, Math.min(STEPS, Math.floor(4e7 / (n * n))));
  for (let step = 0; step < steps; step++) {
    const alpha = 1 - step / steps;
    const settling = step >= steps * 0.8; // last steps: only resolve overlaps
    // Collisions start soft, so linked items can pass one another to meet, then get firm.
    const firmness = Math.min(1, 0.1 + step / (steps * 0.5));
    if (!settling) {
      for (const { i, j, strength } of pairs) {
        const d = sub(p[j], p[i]);
        const distance = length(d) || 1;
        const want = items[i].radius + items[j].radius + gap(i, j);
        if (distance <= want) {
          continue;
        }
        const pull = ((distance - want) / distance) * 0.3 * alpha * strength;
        const share = mass[j] / (mass[i] + mass[j]);
        move(p[i], d, pull * share);
        move(p[j], d, -pull * (1 - share));
      }
      for (const point of p) {
        point.x -= point.x * GRAVITY * alpha;
        point.y -= point.y * GRAVITY * alpha;
        point.z -= point.z * Math.min(0.5, (GRAVITY * alpha) / flat ** 2);
      }
    }
    for (let i = 0; i < n; i++) {
      for (let j = i + 1; j < n; j++) {
        const d = sub(p[j], p[i]);
        let distance = length(d);
        const want = items[i].radius + items[j].radius + gap(i, j);
        if (distance >= want) {
          continue;
        }
        if (distance < 1e-6) {
          // Same point: part along a direction seeded by the pair.
          Object.assign(d, seedPoint(`${items[i].key}|${items[j].key}`));
          distance = length(d);
        }
        const push = ((want - distance) / distance) * firmness;
        const share = mass[j] / (mass[i] + mass[j]);
        move(p[i], d, -push * share);
        move(p[j], d, push * (1 - share));
      }
    }
  }
  // Centre the pack on its middle.
  const box = (axis: keyof Vec3) => {
    const low = Math.min(...p.map((q, i) => q[axis] - items[i].radius));
    const high = Math.max(...p.map((q, i) => q[axis] + items[i].radius));
    return (low + high) / 2;
  };
  const middle = { x: box("x"), y: box("y"), z: box("z") };
  return p.map((q) => sub(q, middle));
}

/**
 * The control points of the curve for a synapse: from the source, up through the centres of the
 * regions holding it (innermost first) to just below the region holding both ends, then down
 * to the target. Empty when both are in the same innermost region: a straight line.
 */
export function route(layout: Layout, source: string, target: string): Vec3[] {
  const ca = layout.chains.get(source);
  const cb = layout.chains.get(target);
  if (!ca || !cb) {
    return [];
  }
  const common = commonLength(ca, cb);
  const centre = (id: string) => layout.regions.get(id)!.center;
  return [...ca.slice(common).reverse().map(centre), ...cb.slice(common).map(centre)];
}

/**
 * Samples a bundled curve from `start` to `end` through `via`: a clamped cubic B-spline,
 * straightened by `beta` (1: through the regions' centres, 0: a straight line), as in
 * Holten's hierarchical edge bundling.
 */
export function bundle(start: Vec3, via: Vec3[], end: Vec3, beta = 0.97, perSpan = 5): Vec3[] {
  if (!via.length) {
    return [start, end];
  }
  const raw = [start, ...via, end];
  const last = raw.length - 1;
  const control = raw.map((point, i) => {
    const t = i / last;
    const straight = { x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t, z: start.z + (end.z - start.z) * t };
    return lerp(straight, point, beta);
  });
  const points = [control[0], control[0], ...control, control[last], control[last]];
  const out: Vec3[] = [];
  for (let span = 0; span + 3 < points.length; span++) {
    const [p0, p1, p2, p3] = points.slice(span, span + 4);
    for (let k = 0; k < perSpan; k++) {
      const t = k / perSpan;
      const t2 = t * t;
      const t3 = t2 * t;
      const b0 = (1 - t) ** 3 / 6;
      const b1 = (3 * t3 - 6 * t2 + 4) / 6;
      const b2 = (-3 * t3 + 3 * t2 + 3 * t + 1) / 6;
      const b3 = t3 / 6;
      out.push({
        x: b0 * p0.x + b1 * p1.x + b2 * p2.x + b3 * p3.x,
        y: b0 * p0.y + b1 * p1.y + b2 * p2.y + b3 * p3.y,
        z: b0 * p0.z + b1 * p1.z + b2 * p2.z + b3 * p3.z,
      });
    }
  }
  out[0] = { ...start };
  out.push({ ...end });
  return out;
}

/**
 * A hue (0-360) for a neuron or a region, from the regions holding it: top-level regions far
 * apart, the regions inside one around its hue. Regions holding everything (the root, the
 * module of a single file) don't count: their insides get the colors.
 */
export function regionHue(layout: Layout, id: string): number {
  const all = layout.regions.get("")?.size ?? 0;
  const chain = (layout.chains.get(id) ?? regionChain(layout, id)).filter((r, i, c) => i === c.length - 1 || layout.regions.get(r)!.size < all);
  const base = hash(chain[0] ?? id) % 360;
  return chain[1] === undefined ? base : (base + (hash(chain[1]) % 90) - 45 + 360) % 360;
}

/** The neurons inside each region, at any depth. */
export function regionMembers(layout: Layout): Map<string, string[]> {
  const members = new Map<string, string[]>();
  for (const [neuron, chain] of layout.chains) {
    for (const region of chain) {
      const list = members.get(region);
      if (list) {
        list.push(neuron);
      } else {
        members.set(region, [neuron]);
      }
    }
  }
  return members;
}

/**
 * Where a name goes just above some points (a region's neurons), seen with the camera's `right`
 * and `up` axes: in the middle across, `gap` above the highest one, at their middle depth. A
 * region's packed sphere can be much bigger than where its neurons are (a few small modules
 * around a big one), so names go by the neurons.
 */
export function labelAnchor(points: Vec3[], right: Vec3, up: Vec3, gap = 0): Vec3 {
  const dot = (a: Vec3, b: Vec3) => a.x * b.x + a.y * b.y + a.z * b.z;
  const forward = { x: right.y * up.z - right.z * up.y, y: right.z * up.x - right.x * up.z, z: right.x * up.y - right.y * up.x };
  let [left, rightmost, top, near, far] = [Infinity, -Infinity, -Infinity, Infinity, -Infinity];
  for (const p of points) {
    const across = dot(p, right);
    const depth = dot(p, forward);
    left = Math.min(left, across);
    rightmost = Math.max(rightmost, across);
    top = Math.max(top, dot(p, up));
    near = Math.min(near, depth);
    far = Math.max(far, depth);
  }
  if (!points.length) {
    return origin();
  }
  const across = (left + rightmost) / 2;
  const depth = (near + far) / 2;
  const height = top + gap;
  return {
    x: right.x * across + up.x * height + forward.x * depth,
    y: right.y * across + up.y * height + forward.y * depth,
    z: right.z * across + up.z * height + forward.z * depth,
  };
}

/** Region ids from the root to `id`. */
export function regionChain(layout: Layout, id: string): string[] {
  const chain: string[] = [];
  for (let region = layout.regions.get(id); region; region = region.parent !== undefined ? layout.regions.get(region.parent) : undefined) {
    chain.unshift(region.id);
  }
  return chain;
}

// Vectors

function origin(): Vec3 {
  return { x: 0, y: 0, z: 0 };
}

function add(a: Vec3, b: Vec3): Vec3 {
  return { x: a.x + b.x, y: a.y + b.y, z: a.z + b.z };
}

function sub(a: Vec3, b: Vec3): Vec3 {
  return { x: a.x - b.x, y: a.y - b.y, z: a.z - b.z };
}

function scale(a: Vec3, k: number): Vec3 {
  return { x: a.x * k, y: a.y * k, z: a.z * k };
}

function lerp(a: Vec3, b: Vec3, t: number): Vec3 {
  return { x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, z: a.z + (b.z - a.z) * t };
}

export function length(a: Vec3): number {
  return Math.hypot(a.x, a.y, a.z);
}

/** a += d * k, in place. */
function move(a: Vec3, d: Vec3, k: number): void {
  a.x += d.x * k;
  a.y += d.y * k;
  a.z += d.z * k;
}
