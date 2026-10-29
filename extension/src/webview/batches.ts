/**
 * The brain's spheres and lines, each drawn in one batch. A big project has thousands of each:
 * drawn one by one, the cost is in the draw calls, not in what is drawn, and turning the camera
 * stutters. Each neuron and synapse keeps its own object for 3d-force-graph (its position, and
 * hover and clicks, which raycast it), with a material that is never drawn.
 */

import * as THREE from "three";
import type { Vec3 } from "./layout";

/** For the objects 3d-force-graph keeps per neuron and synapse: raycast, never drawn. */
export const HIDDEN = new THREE.MeshBasicMaterial({ visible: false });
export const HIDDEN_LINE = new THREE.LineBasicMaterial({ visible: false });
/** One unit sphere, scaled to each neuron. */
export const UNIT_SPHERE = new THREE.SphereGeometry(1, 16, 12);

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
  private matrix = new THREE.Matrix4();
  private position = new THREE.Vector3();
  private scale = new THREE.Vector3();
  private rotation = new THREE.Quaternion();

  constructor(private readonly parent: THREE.Object3D) {}

  /** Draws `spheres` where they are now, in their colors. Called each frame. */
  update(spheres: Sphere[]): void {
    if (!this.mesh || this.mesh.instanceMatrix.count < spheres.length) {
      this.dispose();
      const capacity = Math.max(16, Math.ceil(spheres.length * 1.25)); // room for neurons found at runtime
      this.mesh = new THREE.InstancedMesh(UNIT_SPHERE, new THREE.MeshLambertMaterial(), capacity);
      this.mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      this.mesh.frustumCulled = false; // instances move: the mesh's own bounds would be stale
      this.parent.add(this.mesh);
    }
    const mesh = this.mesh;
    let i = 0;
    for (const sphere of spheres) {
      if (sphere.parts) {
        this.position.set(sphere.x ?? 0, sphere.y ?? 0, sphere.z ?? 0);
        this.scale.setScalar(sphere.parts.radius);
        mesh.setMatrixAt(i, this.matrix.compose(this.position, this.rotation, this.scale));
        mesh.setColorAt(i, sphere.parts.color);
        i++;
      }
    }
    mesh.count = i;
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) {
      mesh.instanceColor.needsUpdate = true;
    }
  }

  dispose(): void {
    if (this.mesh) {
      this.mesh.removeFromParent();
      (this.mesh.material as THREE.Material).dispose();
      this.mesh.dispose();
      this.mesh = undefined;
    }
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
  private offsets: number[] = [];
  /** Set when a line moved: positions are written again on the next update. */
  moved = true;

  constructor(private readonly parent: THREE.Object3D) {}

  update(polylines: Polyline[]): void {
    if (!this.lines || polylines.length !== this.drawn.length || polylines.some((line, i) => line !== this.drawn[i] || line.points !== this.drawnPoints[i])) {
      this.build(polylines);
    }
    const geometry = this.lines!.geometry;
    if (this.moved) {
      this.moved = false;
      const positions = geometry.getAttribute("position") as THREE.BufferAttribute;
      const array = positions.array as Float32Array;
      polylines.forEach((line, i) => {
        let at = this.offsets[i] * 3;
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
        const end = this.offsets[i + 1] * 3;
        const start = points[0] ?? { x: 0, y: 0, z: 0 };
        while (at < end) {
          array[at++] = start.x;
          array[at++] = start.y;
          array[at++] = start.z;
        }
      });
      positions.needsUpdate = true;
    }
    const colors = geometry.getAttribute("color") as THREE.BufferAttribute;
    const array = colors.array as Float32Array;
    let changed = false;
    polylines.forEach((line, i) => {
      const at = this.offsets[i] * 4;
      if (!line.color || line.opacity === undefined || at === this.offsets[i + 1] * 4) {
        return;
      }
      const { r, g, b } = line.color;
      if (array[at] === r && array[at + 1] === g && array[at + 2] === b && array[at + 3] === line.opacity) {
        return;
      }
      changed = true;
      for (let v = at, end = this.offsets[i + 1] * 4; v < end; v += 4) {
        array[v] = r;
        array[v + 1] = g;
        array[v + 2] = b;
        array[v + 3] = line.opacity;
      }
    });
    if (changed) {
      colors.needsUpdate = true;
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
    // Opacity in the fourth component; NaN so the first update writes every line's color.
    geometry.setAttribute("color", new THREE.BufferAttribute(new Float32Array(vertices * 4).fill(NaN), 4).setUsage(THREE.DynamicDrawUsage));
    // Hairlines that don't hide one another: tracts read by how many lines run together.
    const material = new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false });
    this.lines = new THREE.LineSegments(geometry, material);
    this.lines.frustumCulled = false;
    this.parent.add(this.lines);
    this.drawn = polylines.slice();
    this.drawnPoints = polylines.map((line) => line.points);
    this.moved = true;
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
