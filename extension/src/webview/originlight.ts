/**
 * An origin path on the brain ("Where did this value come from?", ../origin.ts): its functions
 * stand out (main.ts dims the others, as a focus does: filters.ts, originShades), and each move
 * of the value from one function to the next traces itself along their line in the accent, the
 * way live calls do (flow.ts), from the value's sources to the question. Unlike a call, the
 * strokes stay drawn until the path is cleared. All of them are one line object: one draw call.
 */

import * as THREE from "three";
import type { OriginPath } from "../origin";
import { alongPolyline, crossingTime, ease, partOf } from "./flow";
import type { Vec3 } from "./layout";
import type { NeuronNode, SynapseLink } from "./objects";
import { ACCENT } from "./theme";

/** Seconds between the sweeps of two moves one step apart: the light travels toward the question. */
const STAGGER = 0.3;
const OPACITY = 0.95;
/** Seconds a step's neuron pulses when the step is selected, and how many times. */
const PULSES = 3;
const PULSE_EVERY = 0.45;

interface Stroke {
  points: Vec3[];
  along: number[];
  start: number;
  crossing: number;
}

export class OriginLight {
  path: OriginPath | undefined;
  /** The functions of the path. */
  members = new Set<string>();
  private strokes: Stroke[] = [];
  private readonly lines: THREE.LineSegments<THREE.BufferGeometry, THREE.LineBasicMaterial>;
  private shownAt = 0;
  private pulse: { node: string; next: number; left: number } | undefined;

  constructor(scene: THREE.Object3D) {
    const material = new THREE.LineBasicMaterial({ color: ACCENT, transparent: true, opacity: OPACITY, depthWrite: false });
    this.lines = new THREE.LineSegments(new THREE.BufferGeometry(), material);
    this.lines.frustumCulled = false;
    this.lines.renderOrder = 0.5; // over the grey lines
    this.lines.visible = false;
    scene.add(this.lines);
  }

  get start(): string | undefined {
    return this.path?.start;
  }

  show(path: OriginPath, now: number): void {
    this.path = path;
    this.members = new Set(path.nodes);
    this.shownAt = now;
    this.strokes = [];
    this.pulse = undefined;
  }

  clear(): void {
    this.path = undefined;
    this.members = new Set();
    this.strokes = [];
    this.pulse = undefined;
    this.lines.visible = false;
  }

  /** Lines are placed again (a new graph, a neuron moved): `route` gives each move's line, from where the value was to where it went. */
  place(route: (from: string, to: string) => Vec3[] | undefined): void {
    const path = this.path;
    if (!path) {
      return;
    }
    const deepest = Math.max(1, ...path.links.map((link) => link.depth));
    this.strokes = path.links.flatMap((link) => {
      const points = route(link.from, link.to);
      if (!points || points.length < 2) {
        return [];
      }
      const { along, length } = alongPolyline(points);
      return [{ points, along, start: this.shownAt + (deepest - link.depth) * STAGGER, crossing: crossingTime(length) }];
    });
  }

  /** The step's neuron pulses a few times: `light` sets its heat, which main.ts fades. */
  pulseNode(node: string, now: number): void {
    this.pulse = { node, next: now, left: PULSES };
  }

  /** Draws the strokes as far as they got at `now` (seconds), and pulses; true while something still moves. */
  draw(now: number, light: (node: string) => void): boolean {
    let moving = false;
    const pulse = this.pulse;
    if (pulse) {
      if (now >= pulse.next) {
        light(pulse.node);
        pulse.left--;
        pulse.next = now + PULSE_EVERY;
      }
      if (pulse.left <= 0) {
        this.pulse = undefined;
      }
      moving = true;
    }
    if (!this.strokes.length) {
      this.lines.visible = false;
      return moving;
    }
    const segments: number[] = [];
    for (const stroke of this.strokes) {
      const head = ease((now - stroke.start) / stroke.crossing);
      if (head < 1) {
        moving = true;
      }
      if (head <= 0) {
        continue;
      }
      const drawn = partOf(stroke.points, stroke.along, head);
      for (let i = 1; i < drawn.length; i++) {
        const [a, b] = [drawn[i - 1], drawn[i]];
        segments.push(a.x, a.y, a.z, b.x, b.y, b.z);
      }
    }
    const geometry = this.lines.geometry;
    const position = geometry.getAttribute("position") as THREE.BufferAttribute | undefined;
    if (!position || position.array.length < segments.length) {
      geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(Math.max(segments.length, 6) * 2), 3));
    }
    const positions = geometry.getAttribute("position") as THREE.BufferAttribute;
    (positions.array as Float32Array).set(segments);
    positions.needsUpdate = true;
    geometry.setDrawRange(0, segments.length / 3);
    this.lines.visible = segments.length > 0;
    return moving;
  }
}

/**
 * The line a value moved along from function `from` to `to`: their synapse, a call's before
 * shared state's, turned to run from `from`; else a straight line between them.
 */
export function routeBetween(from: string, to: string, links: SynapseLink[], nodes: Map<string, NeuronNode>): Vec3[] | undefined {
  let found: SynapseLink | undefined;
  for (const link of links) {
    const { source, target } = link.synapse;
    if (link.path && ((source === from && target === to) || (source === to && target === from)) && (!found || found.synapse.kind === "state")) {
      found = link;
    }
  }
  if (found?.path) {
    return found.synapse.source === from ? found.path.points : [...found.path.points].reverse();
  }
  const ends = [nodes.get(from), nodes.get(to)];
  return ends[0] && ends[1] ? ends.map((n) => ({ x: n!.x ?? 0, y: n!.y ?? 0, z: n!.z ?? 0 })) : undefined;
}
