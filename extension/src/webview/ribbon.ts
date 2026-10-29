/**
 * A glowing ribbon over a synapse, for the waves of light of flow.ts: a strip of a fixed width on
 * screen that follows the line (straight, or a tract's curve), drawn additively by a small
 * shader that lights it where the waves are. Only synapses with waves on them have one.
 */

import * as THREE from "three";
import { MAX_WAVES } from "./flow";
import type { Vec3 } from "./layout";

/** A ribbon's width on screen in CSS pixels, glow included. */
export const RIBBON_WIDTH = 30;

/** Uniforms every ribbon shares: the drawing size, and the ribbon's width in its pixels. */
export const ribbonShared = {
  resolution: { value: new THREE.Vector2(1, 1) },
  width: { value: RIBBON_WIDTH },
  /** Dims every ribbon when many are lit (crowdGain). */
  gain: { value: 1 },
};

const vertexShader = /* glsl */ `
  attribute vec3 previous;
  attribute vec3 next;
  attribute float side;
  attribute float along;
  uniform vec2 resolution;
  uniform float width;
  varying float vAlong;
  varying float vSide;

  void main() {
    mat4 toClip = projectionMatrix * modelViewMatrix;
    vec4 clip = toClip * vec4(position, 1.0);
    vec4 a = toClip * vec4(previous, 1.0);
    vec4 b = toClip * vec4(next, 1.0);
    // Across the line on screen, with x and y in the same units (aspect).
    vec2 aspect = vec2(resolution.x / resolution.y, 1.0);
    vec2 d = (b.xy / max(b.w, 1e-4) - a.xy / max(a.w, 1e-4)) * aspect;
    vec2 dir = length(d) > 1e-6 ? normalize(d) : vec2(1.0, 0.0);
    vec2 normal = vec2(-dir.y, dir.x) / aspect;
    clip.xy += normal * side * width / resolution.y * clip.w;
    vAlong = along;
    vSide = side;
    gl_Position = clip;
  }
`;

// The same light as brightness() in flow.ts, times a soft profile across the ribbon: a thin
// bright core in a wider body and a faint halo, whitening where the light is strongest.
const fragmentShader = /* glsl */ `
  uniform vec3 color;
  uniform vec2 waves[${MAX_WAVES}];
  uniform float tail;
  uniform float glow;
  uniform float gain;
  varying float vAlong;
  varying float vSide;

  float profile(float s, float head) {
    float behind = head - s;
    if (behind < 0.0) {
      float front = behind / (0.12 * tail);
      return exp(-front * front);
    }
    float left = max(0.0, 1.0 - behind / tail);
    return left * left;
  }

  void main() {
    float sum = glow;
    for (int i = 0; i < ${MAX_WAVES}; i++) {
      sum += waves[i].y * profile(vAlong, waves[i].x);
    }
    float light = 1.0 - exp(-2.0 * sum);
    float across = vSide * vSide;
    float core = exp(-across * 20.0);
    float body = exp(-across * 5.0);
    float halo = exp(-across * 1.6);
    float alpha = light * (0.9 * core + 0.55 * body + 0.3 * halo);
    vec3 tint = mix(color, vec3(1.0), 0.45 * core * light * light);
    gl_FragColor = vec4(tint, alpha * gain);
  }
`;

export interface Ribbon {
  mesh: THREE.Mesh<THREE.BufferGeometry, THREE.ShaderMaterial>;
  /** Number of points it was built for: a line with another count needs a new geometry. */
  count: number;
}

/** A ribbon along `points` (with `along`, their positions 0 to 1), lit in `color`. */
export function makeRibbon(points: Vec3[], along: number[], color: THREE.Color): Ribbon {
  const n = points.length;
  const geometry = new THREE.BufferGeometry();
  for (const name of ["position", "previous", "next"]) {
    geometry.setAttribute(name, new THREE.BufferAttribute(new Float32Array(n * 2 * 3), 3));
  }
  const side = new Float32Array(n * 2);
  for (let i = 0; i < n; i++) {
    side[2 * i] = -1;
    side[2 * i + 1] = 1;
  }
  geometry.setAttribute("side", new THREE.BufferAttribute(side, 1));
  geometry.setAttribute("along", new THREE.BufferAttribute(new Float32Array(n * 2), 1));
  const index: number[] = [];
  for (let i = 0; i + 1 < n; i++) {
    index.push(2 * i, 2 * i + 1, 2 * i + 2, 2 * i + 1, 2 * i + 3, 2 * i + 2);
  }
  geometry.setIndex(index);
  const material = new THREE.ShaderMaterial({
    vertexShader,
    fragmentShader,
    uniforms: {
      ...ribbonShared,
      color: { value: color },
      waves: { value: Array.from({ length: MAX_WAVES }, () => new THREE.Vector2()) },
      tail: { value: 0.3 },
      glow: { value: 0 },
    },
    transparent: true,
    // Light shows through the neurons in front, as the sparks of lit functions do.
    depthTest: false,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.DoubleSide,
  });
  const mesh = new THREE.Mesh(geometry, material);
  // The shader moves vertices across the line: the bounding sphere of the points is not enough.
  mesh.frustumCulled = false;
  mesh.renderOrder = 0.5; // over the neurons, under labels
  const ribbon = { mesh, count: n };
  placeRibbon(ribbon, points, along);
  return ribbon;
}

/** Moves a ribbon onto its line's points (same count as it was built for). */
export function placeRibbon(ribbon: Ribbon, points: Vec3[], along: number[]): void {
  const geometry = ribbon.mesh.geometry;
  const attribute = (name: string) => geometry.getAttribute(name) as THREE.BufferAttribute;
  const [position, previous, next, at] = ["position", "previous", "next", "along"].map(attribute);
  const n = points.length;
  points.forEach((p, i) => {
    const before = points[Math.max(i - 1, 0)];
    const after = points[Math.min(i + 1, n - 1)];
    for (const k of [2 * i, 2 * i + 1]) {
      position.setXYZ(k, p.x, p.y, p.z);
      previous.setXYZ(k, before.x, before.y, before.z);
      next.setXYZ(k, after.x, after.y, after.z);
      at.setX(k, along[i]);
    }
  });
  for (const a of [position, previous, next, at]) {
    a.needsUpdate = true;
  }
}

/** Sets the waves a ribbon shows (head and strength of each; missing ones are dark). */
export function lightRibbon(ribbon: Ribbon, waves: { head: number; strength: number }[], tail: number, glow: number): void {
  const uniforms = ribbon.mesh.material.uniforms;
  (uniforms.waves.value as THREE.Vector2[]).forEach((v, i) => v.set(waves[i]?.head ?? 0, waves[i]?.strength ?? 0));
  uniforms.tail.value = tail;
  uniforms.glow.value = glow;
}

export function disposeRibbon(ribbon: Ribbon): void {
  ribbon.mesh.removeFromParent();
  ribbon.mesh.geometry.dispose();
  ribbon.mesh.material.dispose();
}
