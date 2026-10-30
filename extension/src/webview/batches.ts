/**
 * The brain's spheres and lines, each drawn in one batch. A big project has thousands of each:
 * drawn one by one, the cost is in the draw calls, not in what is drawn, and turning the camera
 * stutters. Each neuron and synapse keeps its own object for 3d-force-graph (its position, and
 * hover and clicks, which raycast it), with a material that is never drawn.
 *
 * Only what changed is written and uploaded: a still brain costs nothing, and a dragged neuron
 * costs its own sphere and lines, not the whole brain's.
 */

import * as THREE from "three";
import type { Vec3 } from "./layout";

/**
 * For the objects 3d-force-graph keeps per neuron and synapse: raycast, never drawn. Shared by
 * all of them, and 3d-force-graph disposes them with each object it removes: harmless, as they
 * never reach the GPU. Nothing drawn uses them (the sphere batch has its own UNIT_SPHERE).
 */
export const HIDDEN = new THREE.MeshBasicMaterial({ visible: false });
export const HIDDEN_LINE = new THREE.LineBasicMaterial({ visible: false });
/** A neuron's sphere for hover and clicks, scaled to it. */
export const PICK_SPHERE = new THREE.SphereGeometry(1, 16, 12);
/** One unit sphere, scaled to each neuron, drawn by the sphere batch. */
const UNIT_SPHERE = new THREE.SphereGeometry(1, 16, 12);

export interface Sphere {
  x?: number;
  y?: number;
  z?: number;
  /** Undefined until 3d-force-graph made the neuron's object: not drawn yet. */
  parts?: { radius: number; color: THREE.Color };
}

/** Every neuron's sphere, as instances of one mesh. */
export class SphereBatch {
  private mesh?: THREE.InstancedMesh;
  private drawn: Sphere[] = [];
  /** Where each was last drawn, x y z: only spheres that moved are written again. */
  private placed = new Float64Array(0);
  private matrix = new THREE.Matrix4();
  private position = new THREE.Vector3();
  private scale = new THREE.Vector3();
  private rotation = new THREE.Quaternion();

  constructor(private readonly parent: THREE.Object3D) {}

  /** Draws `spheres`: their places again when some `moved`, their colors when some were `recolored`. */
  update(spheres: Sphere[], moved: boolean, recolored: boolean): void {
    const ready = spheres.length === this.drawn.length && spheres.every((sphere, i) => sphere === this.drawn[i] && sphere.parts) ? this.drawn : spheres.filter((sphere) => sphere.parts);
    const changed = ready !== this.drawn;
    if (!this.mesh || this.mesh.instanceMatrix.count < ready.length) {
      this.dispose();
      const capacity = Math.max(16, Math.ceil(ready.length * 1.25)); // room for neurons found at runtime
      this.mesh = new THREE.InstancedMesh(UNIT_SPHERE, new THREE.MeshLambertMaterial(), capacity);
      this.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      this.mesh.frustumCulled = false; // instances move: the mesh's own bounds would be stale
      this.parent.add(this.mesh);
    }
    const mesh = this.mesh;
    this.drawn = ready;
    mesh.count = ready.length;
    if (changed) {
      this.placed = new Float64Array(ready.length * 3).fill(NaN);
    }
    if (changed || moved) {
      let low = Infinity;
      let high = -1;
      ready.forEach((sphere, i) => {
        const [x, y, z] = [sphere.x ?? 0, sphere.y ?? 0, sphere.z ?? 0];
        if (this.placed[i * 3] === x && this.placed[i * 3 + 1] === y && this.placed[i * 3 + 2] === z) {
          return;
        }
        this.placed.set([x, y, z], i * 3);
        this.position.set(x, y, z);
        this.scale.setScalar(sphere.parts!.radius);
        mesh.setMatrixAt(i, this.matrix.compose(this.position, this.rotation, this.scale));
        low = Math.min(low, i);
        high = i;
      });
      if (high >= 0) {
        mesh.instanceMatrix.clearUpdateRanges();
        mesh.instanceMatrix.addUpdateRange(low * 16, (high - low + 1) * 16);
        mesh.instanceMatrix.needsUpdate = true;
      }
    }
    if ((changed || recolored) && ready.length) {
      ready.forEach((sphere, i) => mesh.setColorAt(i, sphere.parts!.color));
      mesh.instanceColor!.needsUpdate = true;
    }
  }

  dispose(): void {
    if (this.mesh) {
      this.mesh.removeFromParent();
      (this.mesh.material as THREE.Material).dispose();
      this.mesh.dispose();
      this.mesh = undefined;
    }
    this.drawn = [];
  }
}

export interface Polyline {
  /** Where it runs, as last placed (undefined until then: not drawn). */
  path?: { points: Vec3[] };
  /** How many points its path has: room for them in the batch (undefined until its object is made). */
  points?: number;
  color?: THREE.Color;
  opacity?: number;
}

/** Every synapse's line, as segments of one geometry, each vertex with its line's color and opacity. */
export class LineBatch {
  private lines?: THREE.LineSegments<THREE.BufferGeometry, THREE.LineBasicMaterial>;
  private drawn: Polyline[] = [];
  private drawnPoints: (number | undefined)[] = [];
  private index = new Map<Polyline, number>();
  private offsets: number[] = [];
  private moved = new Set<Polyline>();
  private recolored = new Set<Polyline>();
  private all = true;

  constructor(private readonly parent: THREE.Object3D) {}

  /** `line` was placed again: written with the next update. */
  move(line: Polyline): void {
    this.moved.add(line);
  }

  /** `line` changed color or opacity: written with the next update. */
  recolor(line: Polyline): void {
    this.recolored.add(line);
  }

  /** Every line written again with the next update (they were all restyled). */
  rewriteAll(): void {
    this.all = true;
  }

  update(polylines: Polyline[]): void {
    if (!this.lines || polylines.length !== this.drawn.length || polylines.some((line, i) => line !== this.drawn[i] || line.points !== this.drawnPoints[i])) {
      this.build(polylines);
    }
    const geometry = this.lines!.geometry;
    const positions = geometry.getAttribute("position") as THREE.BufferAttribute;
    const colors = geometry.getAttribute("color") as THREE.BufferAttribute;
    if (this.all) {
      polylines.forEach((line, i) => {
        this.writePositions(line, i);
        this.writeColor(line, i);
      });
      positions.clearUpdateRanges();
      colors.clearUpdateRanges();
      positions.needsUpdate = true;
      colors.needsUpdate = true;
      this.all = false;
    } else {
      this.upload(this.moved, positions, 3, (line, i) => this.writePositions(line, i));
      this.upload(this.recolored, colors, 4, (line, i) => this.writeColor(line, i));
    }
    this.moved.clear();
    this.recolored.clear();
  }

  /** Writes `lines` and uploads only their parts of `attribute` (`size` numbers per vertex). */
  private upload(lines: Set<Polyline>, attribute: THREE.BufferAttribute, size: number, write: (line: Polyline, i: number) => void): void {
    if (!lines.size) {
      return;
    }
    attribute.clearUpdateRanges();
    for (const line of lines) {
      const i = this.index.get(line);
      if (i !== undefined && this.offsets[i + 1] > this.offsets[i]) {
        write(line, i);
        attribute.addUpdateRange(this.offsets[i] * size, (this.offsets[i + 1] - this.offsets[i]) * size);
      }
    }
    attribute.needsUpdate = true;
  }

  private writePositions(line: Polyline, i: number): void {
    const array = (this.lines!.geometry.getAttribute("position") as THREE.BufferAttribute).array as Float32Array;
    let at = this.offsets[i] * 3;
    const end = this.offsets[i + 1] * 3;
    const points = line.path?.points ?? [];
    const count = Math.min(points.length, line.points ?? 0);
    for (let p = 0; p + 1 < count; p++) {
      for (const q of [points[p], points[p + 1]]) {
        array[at++] = q.x;
        array[at++] = q.y;
        array[at++] = q.z;
      }
    }
    // Points this line has no use for (a shorter path than planned) collapse onto its start.
    const start = points[0] ?? { x: 0, y: 0, z: 0 };
    while (at < end) {
      array[at++] = start.x;
      array[at++] = start.y;
      array[at++] = start.z;
    }
  }

  private writeColor(line: Polyline, i: number): void {
    const array = (this.lines!.geometry.getAttribute("color") as THREE.BufferAttribute).array as Float32Array;
    const { r, g, b } = line.color ?? { r: 0, g: 0, b: 0 };
    const opacity = line.opacity ?? 0;
    for (let v = this.offsets[i] * 4, end = this.offsets[i + 1] * 4; v < end; v += 4) {
      array[v] = r;
      array[v + 1] = g;
      array[v + 2] = b;
      array[v + 3] = opacity;
    }
  }

  private build(polylines: Polyline[]): void {
    this.dispose();
    this.offsets = [0];
    for (const line of polylines) {
      this.offsets.push(this.offsets[this.offsets.length - 1] + 2 * Math.max((line.points ?? 0) - 1, 0));
    }
    const vertices = this.offsets[this.offsets.length - 1];
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(vertices * 3), 3).setUsage(THREE.DynamicDrawUsage));
    geometry.setAttribute("color", new THREE.BufferAttribute(new Float32Array(vertices * 4), 4).setUsage(THREE.DynamicDrawUsage));
    // Hairlines that don't hide one another: tracts read by how many lines run together.
    const material = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false });
    this.lines = new THREE.LineSegments(geometry, material);
    this.lines.frustumCulled = false;
    this.parent.add(this.lines);
    this.drawn = polylines.slice();
    this.drawnPoints = polylines.map((line) => line.points);
    this.index = new Map(polylines.map((line, i) => [line, i]));
    this.all = true;
  }

  dispose(): void {
    if (this.lines) {
      this.lines.removeFromParent();
      this.lines.geometry.dispose();
      this.lines.material.dispose();
      this.lines = undefined;
    }
  }
}
