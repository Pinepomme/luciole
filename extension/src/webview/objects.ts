/**
 * The brain's three.js objects: what 3d-force-graph places for each neuron and synapse (never
 * drawn: the batches draw them, hover and clicks raycast them), the rings and breakpoint dots made
 * the first time a neuron needs them, the accent strokes of traced calls, the raycast cache
 * hover relies on, and the neuron under a right-click.
 */

import type { ForceGraph3DInstance } from "3d-force-graph";
import * as THREE from "three";
import type SpriteText from "three-spritetext";
import type { Neuron, Synapse } from "./brain";
import { partOf, Trace } from "./flow";
import { HIDDEN, HIDDEN_LINE, PICK_SPHERE } from "./batches";
import { bundle, Layout, neuronRadius, route, Vec3 } from "./layout";
import { ACCENT, dotTexture, LINE, moduleTint, RED, ringTexture } from "./theme";

export interface NeuronNode {
  id: string;
  neuron: Neuron;
  x?: number;
  y?: number;
  z?: number;
  /** Its place on the map: it is pulled back there (after a drag, or a refresh). */
  home: Vec3;
  /** 1 when the function just ran or is running, fading to 0. */
  heat: number;
  /** What its look last depended on (see animate): a settled neuron is skipped. */
  look?: number;
  /** Drawn by the sphere batch, in `color`. */
  parts?: { group: THREE.Group; color: THREE.Color; base: THREE.Color; ring?: THREE.Sprite; mark?: THREE.Sprite; label?: SpriteText; radius: number };
}

export interface SynapseLink {
  id: string;
  source: string | NeuronNode;
  target: string | NeuronNode;
  synapse: Synapse;
  heat: number;
  /** Centres of the regions the line bends through (none inside a region: a straight line). */
  via: Vec3[];
  /** Its own line, never drawn (hover raycasts it); the line batch draws it in `color`, at `opacity`. */
  line?: THREE.Line<THREE.BufferGeometry, THREE.LineBasicMaterial>;
  points?: number;
  color?: THREE.Color;
  opacity?: number;
  /** Its ends as last placed: an unmoved line is not computed again. */
  ends?: number[];
  /** Where the line runs, as last drawn: points, their positions along it (0 to 1), its length. */
  path?: { points: Vec3[]; along: number[]; length: number };
  /** The latest call traced along it (live mode), and the accent stroke drawn over it. */
  trace?: Trace;
  stroke?: THREE.Line<THREE.BufferGeometry, THREE.LineBasicMaterial>;
}

/** Share of their usual opacity kept by lines between regions. */
const INTER_REGION_OPACITY = 0.3;
/** Lines keep their full opacity up to this many; past it, all fade so that density, not brightness, shows tracts. */
const LINES_AT_FULL = 150;
/** Drawn spheres, as a share of the room the map gives each neuron. */
const SPHERE_SCALE = 0.5;

/**
 * Bumped when the pointer moves, the camera comes to rest or the graph's objects are made (a new
 * graph, synapses found at runtime): what the pointer is over may have changed.
 */
let rayVersion = 0;

/** What the pointer is over may have changed: the next raycast is done again (see below). */
export function raysChanged(): void {
  rayVersion++;
}

/**
 * Hover raycasts every object of the brain, several times a second even with the pointer still
 * (say, while a dragged neuron slides back or the camera eases to a stop): a pass over the same
 * objects answers as the last one did until the pointer moves or the camera comes to rest.
 *
 * The patch is global (DragControls and anything else raycasting share it) and keyed on
 * `objects` and rayVersion only, not on the ray: anything that changes what a ray would hit
 * without moving the pointer must bump rayVersion. Callers get the same Intersection objects.
 */
const intersectObjects = THREE.Raycaster.prototype.intersectObjects;
let lastPass: { objects: unknown; version: number; hits: THREE.Intersection[] } | undefined;
THREE.Raycaster.prototype.intersectObjects = function (this: THREE.Raycaster, objects, recursive, intersects = []) {
  if (!lastPass || lastPass.objects !== objects || lastPass.version !== rayVersion) {
    lastPass = { objects, version: rayVersion, hits: intersectObjects.call(this, objects, recursive, []) };
  }
  intersects.push(...(lastPass.hits as typeof intersects));
  return intersects;
} as typeof intersectObjects;

export function neuronObject(node: NeuronNode): THREE.Object3D {
  rayVersion++; // made with a new graph, its old objects gone: a cached hover would name them
  const radius = neuronRadius(node.neuron.degree) * SPHERE_SCALE;
  const base = moduleTint(node.neuron.module);
  // three.js would compute every object's matrix at every frame (thousands of them, most still):
  // a neuron's are updated by hand when it moves (nodePositionUpdate), its ring and label when
  // they are resized (animate, declutterLabels).
  const group = new THREE.Group();
  group.matrixAutoUpdate = false;
  const hit = new THREE.Mesh(PICK_SPHERE, HIDDEN); // for hover and clicks; the sphere batch draws it
  hit.scale.setScalar(radius);
  hit.updateMatrix();
  hit.matrixAutoUpdate = false;
  group.add(hit);
  node.parts = { group, color: base.clone(), base, radius };
  return group;
}

/** Made the first time a neuron needs them (most never do): fewer objects for three.js to walk. */
function sprite(parts: NonNullable<NeuronNode["parts"]>, material: THREE.SpriteMaterial): THREE.Sprite {
  const sprite = new THREE.Sprite(material);
  sprite.raycast = () => {}; // rings: hover is for the sphere (sprites cost a lot to raycast)
  sprite.matrixAutoUpdate = false; // updated where it is resized
  parts.group.add(sprite);
  return sprite;
}

/**
 * Hovered and clicked (as part of its neuron) while shown, never while hidden: the raycaster
 * ignores `visible`, and most labels and dots are hidden.
 */
export function pickableWhenShown(sprite: THREE.Sprite): void {
  sprite.raycast = function (this: THREE.Sprite, raycaster, intersects) {
    if (this.visible) {
      THREE.Sprite.prototype.raycast.call(this, raycaster, intersects);
    }
  };
}

/**
 * A thin ring around a function that runs (accent), waits on an await (accent, no fill) or
 * where the debugger stopped (red). Sized each frame it shows: never too small to see.
 */
export function ringOf(parts: NonNullable<NeuronNode["parts"]>): THREE.Sprite {
  return (parts.ring ??= sprite(parts, new THREE.SpriteMaterial({ map: ringTexture, color: ACCENT, transparent: true, opacity: 0, depthWrite: false })));
}

/** A red dot on functions that hold a breakpoint, as in the editor's gutter. */
export function markOf(parts: NonNullable<NeuronNode["parts"]>): THREE.Sprite {
  if (!parts.mark) {
    const mark = sprite(parts, new THREE.SpriteMaterial({ map: dotTexture, color: RED, transparent: true, depthWrite: false, depthTest: false }));
    mark.scale.setScalar(Math.max(2, parts.radius * 0.9));
    mark.position.set(parts.radius * 0.9, parts.radius * 0.9, 0);
    mark.renderOrder = 2; // over labels
    mark.updateMatrix();
    pickableWhenShown(mark); // right-click on the dot removes its breakpoints
    parts.mark = mark;
  }
  return parts.mark;
}

/** `count`: how many synapses the brain has (baseOpacity). */
export function synapseObject(link: SynapseLink, layout: Layout | undefined, count: number): THREE.Object3D {
  rayVersion++; // see neuronObject
  const geometry = new THREE.BufferGeometry();
  link.via = layout ? route(layout, link.synapse.source, link.synapse.target) : [];
  const points = bundle({ x: 0, y: 0, z: 0 }, link.via, { x: 0, y: 0, z: 0 }).length;
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(points * 3), 3));
  link.points = points;
  link.color = LINE.clone();
  link.opacity = baseOpacity(link, count);
  link.line = new THREE.Line(geometry, HIDDEN_LINE); // for hover; the line batch draws it
  link.line.matrixAutoUpdate = false; // its points are where it is
  return link.line;
}

/**
 * Lines between regions are fainter: they add up into tracts where many travel together. Big
 * brains (`count` synapses) have fainter lines all over.
 */
export function baseOpacity(link: SynapseLink, count: number): number {
  const opacity = { call: 0.5, state: 0.25, runtime: 0.3, spawn: 0.4 }[link.synapse.kind] * Math.max(0.4, Math.min(1, Math.sqrt(LINES_AT_FULL / Math.max(count, 1))));
  return link.via.length ? opacity * INTER_REGION_OPACITY : opacity;
}

const worldPoint = new THREE.Vector3();
/** World units per screen pixel at an object's distance from the camera. */
export function worldPerPixel(object: THREE.Object3D, camera: THREE.PerspectiveCamera): number {
  const distance = camera.position.distanceTo(object.getWorldPosition(worldPoint));
  const height = document.getElementById("brain")!.clientHeight || 1;
  return (distance * 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2)) / height;
}

const rightRay = new THREE.Raycaster();
/**
 * The neuron drawn at a point of the window: its sphere, or its label or dot when shown; else the
 * nearest within a few pixels, as zoomed out on a big brain spheres are smaller than the pointer.
 */
export function neuronUnder(graph: ForceGraph3DInstance, clientX: number, clientY: number): NeuronNode | undefined {
  const box = document.getElementById("brain")!.getBoundingClientRect();
  const at = new THREE.Vector2(((clientX - box.left) / box.width) * 2 - 1, -((clientY - box.top) / box.height) * 2 + 1);
  rightRay.setFromCamera(at, graph.camera());
  const owners = new Map<THREE.Object3D, NeuronNode>();
  for (const node of graph.graphData().nodes as NeuronNode[]) {
    if (node.parts) {
      owners.set(node.parts.group, node);
    }
  }
  for (const { object } of rightRay.intersectObjects([...owners.keys()], true)) {
    for (let o: THREE.Object3D | null = object; o; o = o.parent) {
      const node = owners.get(o);
      if (node) {
        return node;
      }
    }
  }
  let nearest: NeuronNode | undefined;
  let best = 8 * 8;
  for (const node of owners.values()) {
    const { x, y } = graph.graph2ScreenCoords(node.x!, node.y!, node.z!);
    const d = (box.left + x - clientX) ** 2 + (box.top + y - clientY) ** 2;
    if (d < best && node.neuron.node.kind === "function") {
      [nearest, best] = [node, d];
    }
  }
  return nearest;
}

/** A traced call's accent stroke over its line (`path`, added to `group`), from the caller to `head`, at `opacity`. */
export function drawStroke(link: SynapseLink, path: NonNullable<SynapseLink["path"]>, head: number, opacity: number, group: THREE.Group): void {
  const { points, along } = path;
  if (link.stroke && link.stroke.geometry.getAttribute("position").count < points.length + 1) {
    disposeStroke(link);
  }
  if (!link.stroke) {
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array((points.length + 1) * 3), 3));
    const material = new THREE.LineBasicMaterial({ color: ACCENT, transparent: true, depthWrite: false });
    link.stroke = new THREE.Line(geometry, material);
    link.stroke.frustumCulled = false;
    link.stroke.renderOrder = 0.5; // over its grey line
    group.add(link.stroke);
  }
  const drawn = partOf(points, along, head);
  const positions = link.stroke.geometry.getAttribute("position") as THREE.BufferAttribute;
  drawn.forEach((p, i) => positions.setXYZ(i, p.x, p.y, p.z));
  positions.needsUpdate = true;
  link.stroke.geometry.setDrawRange(0, drawn.length);
  link.stroke.material.opacity = opacity;
}

export function disposeStroke(link: SynapseLink): void {
  if (link.stroke) {
    link.stroke.removeFromParent();
    link.stroke.geometry.dispose();
    link.stroke.material.dispose();
    link.stroke = undefined;
  }
}
