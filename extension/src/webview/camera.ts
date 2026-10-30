/** Camera moves of the brain: framing it (or part of it), and flying to one neuron. */

import type { ForceGraph3DInstance } from "3d-force-graph";
import * as THREE from "three";
import type { NeuronNode } from "./objects";

/**
 * Frames `subset` of the brain from where the camera looks: its extent across and up the
 * screen, so the flat map fills the view, and room for labels (more around `small` brains, whose
 * every name shows).
 */
export function fitCamera(graph: ForceGraph3DInstance, subset: NeuronNode[], small: boolean, duration: number): void {
  const camera = graph.camera() as THREE.PerspectiveCamera;
  const forward = camera.getWorldDirection(new THREE.Vector3());
  const right = new THREE.Vector3().crossVectors(forward, camera.up).normalize();
  const up = new THREE.Vector3().crossVectors(right, forward).normalize();
  const point = new THREE.Vector3();
  const low = [Infinity, Infinity, Infinity];
  const high = [-Infinity, -Infinity, -Infinity];
  for (const node of subset) {
    point.set(node.x ?? 0, node.y ?? 0, node.z ?? 0);
    const r = node.parts?.radius ?? 0;
    [point.dot(right), point.dot(up), point.dot(forward)].forEach((v, i) => {
      low[i] = Math.min(low[i], v - r);
      high[i] = Math.max(high[i], v + r);
    });
  }
  const mid = (i: number) => (low[i] + high[i]) / 2;
  const center = new THREE.Vector3().addScaledVector(right, mid(0)).addScaledVector(up, mid(1)).addScaledVector(forward, mid(2));
  // Room for the labels above packages, and for the names around small brains.
  const margin = small ? 1.3 : 1.15;
  const vertical = (camera.fov / 2) * (Math.PI / 180);
  const horizontal = Math.atan(Math.tan(vertical) * camera.aspect);
  const halfWidth = Math.max((high[0] - low[0]) / 2, 20) * margin;
  const halfHeight = Math.max((high[1] - low[1]) / 2, 20) * margin;
  const distance = Math.max(halfWidth / Math.tan(horizontal), halfHeight / Math.tan(vertical)) + (high[2] - low[2]) / 2;
  const position = center.clone().addScaledVector(forward, -distance);
  graph.cameraPosition({ x: position.x, y: position.y, z: position.z }, { x: center.x, y: center.y, z: center.z }, duration);
}

/** Flies to `node`, from outside the brain (in 800 ms). */
export function flyToNode(graph: ForceGraph3DInstance, node: NeuronNode): void {
  const distance = 80;
  const length = Math.hypot(node.x ?? 0, node.y ?? 0, node.z ?? 0) || 1;
  const ratio = 1 + distance / length;
  graph.cameraPosition(
    { x: (node.x ?? 0) * ratio, y: (node.y ?? 0) * ratio, z: (node.z ?? 0) * ratio },
    { x: node.x ?? 0, y: node.y ?? 0, z: node.z ?? 0 },
    800,
  );
}
