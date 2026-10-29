/**
 * Webview entry point: the 3D brain. Neurons are functions, synapses the variables flowing
 * between them. In live mode, functions light up as they run and pulses travel along synapses.
 */

import ForceGraph3D from "3d-force-graph";
import * as THREE from "three";
import SpriteText from "three-spritetext";
import type { Graph } from "../graph";
import type { FromWebview, LiveFrame, SourceLine, ThreadStack, ToWebview } from "../messages";
import { Brain, breakpointAction, breakpointHint, buildBrain, Neuron, neuronAt, Synapse, SynapseKind, synapseId } from "./brain";
import { bundle, labelAnchor, Layout, layoutBrain, NEURON_GAP, neuronRadius, Region, regionHue, regionMembers, route, Vec3 } from "./layout";

declare function acquireVsCodeApi(): { postMessage(message: FromWebview): void };

interface NeuronNode {
  id: string;
  neuron: Neuron;
  x?: number;
  y?: number;
  z?: number;
  /** Its place on the map: it is pulled back there (after a drag, or a refresh). */
  home: Vec3;
  /** 1 when the function just ran or is running, fading to 0. */
  heat: number;
  parts?: { material: THREE.MeshLambertMaterial; halo: THREE.Sprite; spark: THREE.Sprite; mark: THREE.Sprite; label?: SpriteText; radius: number; base: THREE.Color };
}

interface SynapseLink {
  id: string;
  source: string | NeuronNode;
  target: string | NeuronNode;
  synapse: Synapse;
  heat: number;
  /** Centres of the regions the line bends through (none inside a region: a straight line). */
  via: Vec3[];
  line?: THREE.Line<THREE.BufferGeometry, THREE.LineBasicMaterial>;
  /** Set by three-forcegraph, which moves pulses along it: ours, for bundled lines. */
  __curve?: THREE.Curve<THREE.Vector3> | null;
}

/** A polyline as a three.js curve, so pulses follow bundled lines. */
class PathCurve extends THREE.Curve<THREE.Vector3> {
  constructor(private points: Vec3[]) {
    super();
  }

  override getPoint(t: number, target = new THREE.Vector3()): THREE.Vector3 {
    const at = Math.min(Math.max(t, 0), 1) * (this.points.length - 1);
    const i = Math.min(Math.floor(at), this.points.length - 2);
    const [a, b] = [this.points[i], this.points[i + 1]];
    const k = at - i;
    return target.set(a.x + (b.x - a.x) * k, a.y + (b.y - a.y) * k, a.z + (b.z - a.z) * k);
  }
}

/** Labels are always shown below this many neurons; above, only on lit or hovered ones. */
const ALWAYS_LABEL = 60;
/** Seconds for a flash to fade to about a third. */
const FADE = 0.7;
/** Glow kept by functions that ran during the current live session, once their flash fades. */
const VISITED = 0.12;
const MAX_PULSES_PER_LINK = 3;
/** Share of their usual opacity kept by lines between regions. */
const INTER_REGION_OPACITY = 0.25;
/** Lines of the bottom bar: one per thread, then what is waiting. */
const MAX_PATH_LINES = 5;
/** Above this many shown labels, overlapping ones are not hidden (it costs pairs of labels). */
const MAX_DECLUTTERED = 400;
/** Region labels keep this height on screen, in pixels, like a map's place names. */
const REGION_LABEL_PX = { package: 15, module: 12, class: 11 } as const;
/** A region is named while it spans this many pixels on screen, and not once it fills the view (you are inside). */
const REGION_NAMED_MIN = 70;
const REGION_NAMED_MAX = 1.5;

const vscode = acquireVsCodeApi();
const statusEl = document.getElementById("status")!;
const pathEl = document.getElementById("path")!;
const messageEl = document.getElementById("message")!;
const liveButton = document.getElementById("live") as HTMLButtonElement;
const stopButton = document.getElementById("stop") as HTMLButtonElement;

const css = getComputedStyle(document.body);
const cssColor = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
const background = cssColor("--vscode-editor-background", "#1e1e1e");
const foreground = cssColor("--vscode-editor-foreground", "#cccccc");
/** Behind labels, so they read over spheres and lines. */
const labelBackground = `#${new THREE.Color(background).getHexString()}b3`;
/** Running. */
const AMBER = new THREE.Color("#ffc94d");
/** Suspended on an await. */
const BLUE = new THREE.Color("#5aa9ff");
/** Where the debugger stopped, and breakpoints (VS Code's breakpoint red). */
const RED = new THREE.Color("#f14c4c");
const LINE_COLORS: Record<SynapseKind, THREE.Color> = {
  call: new THREE.Color("#6f8bb3"),
  state: new THREE.Color("#8a6fb3"),
  runtime: new THREE.Color("#d18616"),
  spawn: new THREE.Color("#4fb3a9"),
};
const glowTexture = makeGlowTexture();
const dotTexture = makeDotTexture();

let brain: Brain | undefined;
let layout: Layout | undefined;
/** Package, module and class names, above their regions. */
const regionLabels = new THREE.Group();
/** A faint glow of its color behind each top-level region, like an area on a map. */
const regionGlows = new THREE.Group();
let target: string | undefined;
let nodes: NeuronNode[] = [];
let links: SynapseLink[] = [];
const nodeById = new Map<string, NeuronNode>();
/** Synapses that carry the pulses of live calls (call or runtime) and spawns, by `a>b`. */
const callLinks = new Map<string, SynapseLink>();
const spawnLinks = new Map<string, SynapseLink>();
let hovered: NeuronNode | undefined;
let onStack = new Set<string>();
let waiting = new Set<string>();
/** Functions and synapses used since the live session started. */
const visited = new Set<string>();
let pathway = new Set<SynapseLink>();
let live = false;
/** VS Code's breakpoints, and the functions that hold one. */
let breakpoints: SourceLine[] = [];
let breakpointNodes = new Set<string>();
/** Where the debugger stopped, and the function that holds it. */
let debugStop: SourceLine | undefined;
let stoppedIn: string | undefined;
let callsThisSecond: { t: number; n: number }[] = [];

const graph = new ForceGraph3D(document.getElementById("brain")!)
  .backgroundColor(background)
  .showNavInfo(false)
  .nodeId("id")
  .nodeThreeObject((node: object) => neuronObject(node as NeuronNode))
  .nodeLabel((node: object) => neuronTooltip((node as NeuronNode).neuron))
  .linkThreeObject((link: object) => synapseObject(link as SynapseLink))
  .linkPositionUpdate((object, { start, end }, link) => {
    const { via } = link as SynapseLink;
    const points = bundle(start, via, end);
    const positions = (object as THREE.Line).geometry.getAttribute("position") as THREE.BufferAttribute;
    points.forEach((p, i) => positions.setXYZ(i, p.x, p.y, p.z));
    positions.needsUpdate = true;
    (object as THREE.Line).geometry.computeBoundingSphere();
    (link as SynapseLink).__curve = via.length ? new PathCurve(points) : null;
    return true;
  })
  .linkLabel((link: object) => synapseTooltip((link as SynapseLink).synapse))
  .linkDirectionalParticleColor(() => "#ffc94d")
  .linkDirectionalParticleWidth(2.5)
  .linkDirectionalParticleSpeed(0.025)
  .cooldownTicks(120)
  .d3Force("charge", null)
  .d3Force("center", null)
  .d3Force("home", homeForce())
  .onNodeHover((node: object | null) => {
    hovered = (node as NeuronNode | null) ?? undefined;
    document.body.style.cursor = node ? "pointer" : "";
  })
  .onNodeClick((node: object) => reveal(node as NeuronNode))
  .onNodeRightClick((node: object) => {
    const { neuron } = node as NeuronNode;
    const action = breakpointAction(brain?.neurons ?? [], neuron, breakpoints);
    if (action && "add" in action) {
      vscode.postMessage({ type: "addBreakpoint", file: neuron.node.file, line: action.add });
    } else if (action) {
      vscode.postMessage({ type: "removeBreakpoints", file: neuron.node.file, lines: action.remove });
    }
  });

graph.scene().add(regionLabels, regionGlows);
// The map places neurons: links keep their force only to join their ends to the nodes.
(graph.d3Force("link") as unknown as { strength(k: number): void }).strength(0);

/** Pulls each neuron to its place on the map. They start there: this undoes drags and glides refreshes. */
function homeForce() {
  let pulled: NeuronNode[] = [];
  const force = (alpha: number) => {
    const k = 0.3 * Math.max(alpha, 0.05);
    for (const node of pulled) {
      const n = node as NeuronNode & { vx: number; vy: number; vz: number };
      n.vx += (node.home.x - node.x!) * k;
      n.vy += (node.home.y - node.y!) * k;
      n.vz += (node.home.z - node.z!) * k;
    }
  };
  force.initialize = (nodes: object[]) => {
    pulled = nodes as NeuronNode[];
  };
  return force;
}

let fitted = false;
graph.onEngineStop(() => {
  if (!fitted) {
    fitted = true;
    fit(600);
  }
  document.body.dataset.state = "ready";
});

// Three.js objects

function neuronObject(node: NeuronNode): THREE.Object3D {
  const radius = neuronRadius(node.neuron.degree);
  const base = new THREE.Color().setHSL((layout ? regionHue(layout, node.id) : 0) / 360, 0.35, 0.55);
  const material = new THREE.MeshLambertMaterial({ color: base.clone(), emissive: AMBER, emissiveIntensity: 0 });
  const group = new THREE.Group();
  group.add(new THREE.Mesh(new THREE.SphereGeometry(radius, 20, 14), material));
  const halo = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: glowTexture, color: AMBER, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false }),
  );
  halo.scale.setScalar(radius * 2.5);
  group.add(halo);
  // A spark of constant screen size, so a lit function shows even when zoomed far out.
  const spark = new THREE.Sprite(
    new THREE.SpriteMaterial({ map: glowTexture, color: AMBER, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false, depthTest: false, sizeAttenuation: false }),
  );
  spark.scale.setScalar(0);
  group.add(spark);
  // A red dot on functions that hold a breakpoint, as in the editor's gutter.
  const mark = new THREE.Sprite(new THREE.SpriteMaterial({ map: dotTexture, color: RED, transparent: true, opacity: 0, depthWrite: false, depthTest: false }));
  mark.scale.setScalar(Math.max(2.5, radius * 0.9));
  mark.position.set(radius * 0.85, radius * 0.85, 0);
  mark.renderOrder = 2; // over labels
  group.add(mark);
  node.parts = { material, halo, spark, mark, radius, base };
  return group;
}

function synapseObject(link: SynapseLink): THREE.Object3D {
  const geometry = new THREE.BufferGeometry();
  link.via = layout ? route(layout, link.synapse.source, link.synapse.target) : [];
  const count = bundle({ x: 0, y: 0, z: 0 }, link.via, { x: 0, y: 0, z: 0 }).length;
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(count * 3), 3));
  const material = new THREE.LineBasicMaterial({ color: LINE_COLORS[link.synapse.kind].clone(), transparent: true, opacity: baseOpacity(link) });
  link.line = new THREE.Line(geometry, material);
  return link.line;
}

/** Lines between regions are fainter: they add up into tracts where many travel together. */
function baseOpacity(link: SynapseLink): number {
  const opacity = { call: 0.55, state: 0.3, runtime: 0.2, spawn: 0.3 }[link.synapse.kind];
  return link.via.length ? opacity * INTER_REGION_OPACITY : opacity;
}

/** A label per region (package, module, class), sized by the region; declutterLabels places them. */
function makeRegionLabels(): void {
  for (const child of regionLabels.children) {
    (child as SpriteText).material.map?.dispose();
    (child as SpriteText).material.dispose();
  }
  regionLabels.clear();
  regionGlows.children.forEach((glow) => ((glow as THREE.Sprite).material.dispose()));
  regionGlows.clear();
  const members = layout ? regionMembers(layout) : new Map<string, string[]>();
  for (const region of layout?.regions.values() ?? []) {
    // A region holding the whole brain (a single file) needs neither: it is the brain.
    const whole = region.size === layout!.regions.get("")!.size;
    if (region.depth === 1 && region.size > 1 && !whole) {
      const color = new THREE.Color().setHSL(regionHue(layout!, region.id) / 360, 0.5, 0.5);
      const glow = new THREE.Sprite(
        new THREE.SpriteMaterial({ map: glowTexture, color, transparent: true, opacity: 0.1, blending: THREE.AdditiveBlending, depthWrite: false }),
      );
      glow.position.set(region.center.x, region.center.y, region.center.z);
      glow.scale.setScalar(region.radius * 3);
      regionGlows.add(glow);
    }
    if (region.kind === "root" || whole || (region.kind === "class" && region.size < 3)) {
      continue;
    }
    const color = new THREE.Color().setHSL(regionHue(layout!, region.id) / 360, region.kind === "class" ? 0.25 : 0.5, 0.75);
    const text = new SpriteText(region.label, 10, `#${color.getHexString()}`); // resized each frame to a fixed height on screen
    text.fontFace = css.getPropertyValue("--vscode-font-family").trim() || "sans-serif";
    text.fontWeight = region.kind === "class" ? "normal" : "bold";
    // A dark outline, so names read over the neurons they sit on.
    text.strokeWidth = 0.6;
    text.strokeColor = background;
    text.material.depthTest = false;
    text.material.depthWrite = false;
    text.material.opacity = region.kind === "class" ? 0.6 : 0.8;
    text.renderOrder = 1;
    text.userData = { region, members: members.get(region.id) ?? [], aspect: text.scale.x / text.scale.y };
    regionLabels.add(text);
  }
}

function makeGlowTexture(): THREE.Texture {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 64;
  const context = canvas.getContext("2d")!;
  const gradient = context.createRadialGradient(32, 32, 0, 32, 32, 32);
  gradient.addColorStop(0, "rgba(255,255,255,1)");
  gradient.addColorStop(0.25, "rgba(255,255,255,0.45)");
  gradient.addColorStop(1, "rgba(255,255,255,0)");
  context.fillStyle = gradient;
  context.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(canvas);
}

/** A solid dot with a dark rim, like the editor's breakpoint glyph. */
function makeDotTexture(): THREE.Texture {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 64;
  const context = canvas.getContext("2d")!;
  context.beginPath();
  context.arc(32, 32, 28, 0, 2 * Math.PI);
  context.fillStyle = "#ffffff";
  context.fill();
  context.lineWidth = 5;
  context.strokeStyle = "rgba(0,0,0,0.6)";
  context.stroke();
  return new THREE.CanvasTexture(canvas);
}

function label(node: NeuronNode): SpriteText {
  const parts = node.parts!;
  if (!parts.label) {
    const text = new SpriteText(node.neuron.label, 3.2, foreground);
    text.fontFace = css.getPropertyValue("--vscode-editor-font-family").trim() || "monospace";
    text.backgroundColor = labelBackground;
    text.padding = [0.8, 0.3];
    text.borderRadius = 1;
    // Drawn over the spheres: a neighbour in front no longer hides it (declutterLabels hides
    // labels that overlap a more important one instead).
    text.material.depthTest = false;
    text.material.depthWrite = false;
    text.renderOrder = 1;
    // Just above the sphere and its breakpoint dot, whatever their size.
    text.position.y = Math.max(parts.radius, parts.mark.position.y + parts.mark.scale.y / 2) + 1.5 + text.scale.y / 2;
    parts.halo.parent!.add(text);
    parts.label = text;
  }
  return parts.label;
}

/** Which label stays when two overlap on screen: the hovered one, where the debugger stopped, what runs. */
function labelPriority(node: NeuronNode): number {
  const rank =
    node === hovered ? 5 : node.id === stoppedIn ? 4 : onStack.has(node.id) ? 3 : node.heat > 0.15 || waiting.has(node.id) ? 2 : breakpointNodes.has(node.id) ? 1 : 0;
  return rank * 1000 + node.parts!.radius; // then bigger (more connected) neurons first
}

const corner = new THREE.Vector3();
const cameraRight = new THREE.Vector3();
const cameraUp = new THREE.Vector3();

/**
 * Places region labels above their regions and keeps those of a readable size on screen, then
 * hides labels that overlap a more important one, so the ones left can be read. Region labels
 * come after functions that run, are hovered or hold a breakpoint, and before the others.
 */
function declutterLabels(): void {
  const camera = graph.camera();
  const element = document.getElementById("brain")!;
  const [width, height] = [element.clientWidth, element.clientHeight];
  cameraRight.set(1, 0, 0).applyQuaternion(camera.quaternion);
  cameraUp.set(0, 1, 0).applyQuaternion(camera.quaternion);
  const toScreen = (dx: number, dy: number, from: THREE.Vector3) => {
    corner.copy(from).addScaledVector(cameraRight, dx).addScaledVector(cameraUp, dy).project(camera);
    return { x: ((corner.x + 1) / 2) * width, y: ((1 - corner.y) / 2) * height, z: corner.z };
  };
  const center = new THREE.Vector3();
  const boxOf = (text: SpriteText) => {
    text.getWorldPosition(center);
    const a = toScreen(-text.scale.x / 2, text.scale.y / 2, center);
    const b = toScreen(text.scale.x / 2, -text.scale.y / 2, center);
    if (a.z > 1 || b.z > 1) {
      return undefined; // behind the camera
    }
    return { left: Math.min(a.x, b.x), right: Math.max(a.x, b.x), top: Math.min(a.y, b.y), bottom: Math.max(a.y, b.y) };
  };
  const shown: { text: SpriteText; priority: number }[] = [];
  for (const node of nodes) {
    if (node.parts?.label?.visible) {
      shown.push({ text: node.parts.label, priority: labelPriority(node) });
    }
  }
  const perspective = camera as THREE.PerspectiveCamera;
  const fovScale = 2 * Math.tan(THREE.MathUtils.degToRad(perspective.fov) / 2);
  for (const child of regionLabels.children) {
    const text = child as SpriteText;
    const region = text.userData.region as Region;
    // Pixels per world unit at the region's distance: names keep one height on screen, and show
    // while the region is big enough to be worth naming but doesn't fill the view.
    const distance = Math.max(1, camera.position.distanceTo(center.set(region.center.x, region.center.y, region.center.z)));
    const pixels = height / (distance * fovScale);
    const span = 2 * region.radius * pixels;
    text.visible = span >= REGION_NAMED_MIN && span <= REGION_NAMED_MAX * Math.max(width, height);
    if (!text.visible) {
      continue;
    }
    const tall = REGION_LABEL_PX[region.kind as keyof typeof REGION_LABEL_PX] / pixels;
    text.scale.set(tall * text.userData.aspect, tall, 1);
    if (region.kind === "package") {
      // Above the package's neurons (its modules fill the middle), wherever they are in its disc.
      const points = (text.userData.members as string[]).map((id) => nodeById.get(id)!).filter(Boolean).map((n) => ({ x: n.x ?? 0, y: n.y ?? 0, z: n.z ?? 0 }));
      const { x, y, z } = labelAnchor(points, cameraRight, cameraUp, NEURON_GAP + text.scale.y * 0.6);
      text.position.set(x, y, z);
    } else {
      // Modules and classes are named at their centre.
      text.position.set(region.center.x, region.center.y, region.center.z);
    }
    text.visible = boxOf(text) !== undefined;
    if (text.visible) {
      // Between the functions that matter (1000 and up) and the others (below 100).
      shown.push({ text, priority: 900 - region.depth * 100 + Math.min(region.radius, 999) / 10 });
    }
  }
  if (shown.length < 2 || shown.length > MAX_DECLUTTERED) {
    return;
  }
  const placed: { left: number; right: number; top: number; bottom: number }[] = [];
  for (const { text } of shown.sort((a, b) => b.priority - a.priority)) {
    const box = boxOf(text);
    if (!box) {
      continue;
    }
    if (placed.some((p) => box.left < p.right && p.left < box.right && box.top < p.bottom && p.top < box.bottom)) {
      text.visible = false;
    } else {
      placed.push(box);
    }
  }
}

// Tooltips (text only: names come from the analyzed code)

function escape(text: string): string {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
}

function neuronTooltip(neuron: Neuron): string {
  const name = neuron.node.kind === "module" ? `module ${neuron.label}` : `${neuron.label}(${neuron.params.join(", ")})`;
  const action = breakpointAction(brain?.neurons ?? [], neuron, breakpoints);
  const hint = action ? `<br><span class="dim">right-click: ${escape(breakpointHint(neuron, action))}</span>` : "";
  return `<b>${escape(name)}</b><br><span class="dim">${escape(neuron.node.file)}:${neuron.node.loc.line}</span>${hint}`;
}

function synapseTooltip(synapse: Synapse): string {
  const callTitle = synapse.byName
    ? "probably calls (matched by method name)"
    : synapse.callback
      ? "passes it to be called later (callback, thread, task)"
      : "calls";
  const title = { call: callTitle, state: "shared state", runtime: "call seen at runtime", spawn: "starts a thread or task" }[synapse.kind];
  const variables = synapse.variables.map((v) => escape(v)).join("<br>");
  return `<span class="dim">${title}</span>${variables ? `<br>${variables}` : ""}`;
}

// Animation: heat fades, materials follow

let last = performance.now();
function animate(now: number): void {
  const dt = Math.min((now - last) / 1000, 0.1);
  last = now;
  const fade = Math.exp(-dt / FADE);
  const showAll = nodes.length <= ALWAYS_LABEL;
  for (const node of nodes) {
    const isStopped = node.id === stoppedIn;
    node.heat = onStack.has(node.id) || isStopped ? 1 : Math.max(node.heat * fade, visited.has(node.id) ? VISITED : 0);
    const parts = node.parts;
    if (!parts) {
      continue;
    }
    (parts.mark.material as THREE.SpriteMaterial).opacity = breakpointNodes.has(node.id) ? 0.95 : 0;
    // Waiting on an await shows in blue, unless the function is also running or just ran.
    const isWaiting = waiting.has(node.id) && node.heat < 0.5 && !isStopped;
    const glow = isStopped ? RED : isWaiting ? BLUE : AMBER;
    const level = isWaiting ? 0.6 : node.heat;
    parts.material.color.copy(parts.base).lerp(glow, level);
    parts.material.emissive.copy(glow);
    parts.material.emissiveIntensity = level * 0.5;
    const halo = parts.halo.material as THREE.SpriteMaterial;
    halo.color.copy(glow);
    halo.opacity = level * 0.55;
    parts.halo.scale.setScalar(parts.radius * (2.5 + 2 * level));
    const spark = isWaiting ? 0.5 : Math.max(0, node.heat - VISITED) / (1 - VISITED);
    const sparkMaterial = parts.spark.material as THREE.SpriteMaterial;
    sparkMaterial.color.copy(glow);
    sparkMaterial.opacity = spark;
    parts.spark.scale.setScalar(0.05 * spark);
    const wantsLabel = showAll || node.heat > 0.15 || isWaiting || node === hovered || breakpointNodes.has(node.id);
    if (wantsLabel || parts.label) {
      label(node).visible = wantsLabel;
    }
  }
  declutterLabels();
  for (const link of links) {
    link.heat = pathway.has(link) ? 1 : Math.max(link.heat * fade, visited.has(link.id) ? VISITED : 0);
    if (link.line) {
      link.line.material.color.copy(LINE_COLORS[link.synapse.kind]).lerp(AMBER, link.heat);
      link.line.material.opacity = baseOpacity(link) + (1 - baseOpacity(link)) * link.heat;
    }
  }
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

// Data

function load(graphData: Graph, newTarget: string): void {
  const sameTarget = newTarget === target;
  target = newTarget;
  brain = buildBrain(graphData);
  // Small brains show every name: room for them.
  const gap = brain.neurons.length <= ALWAYS_LABEL ? 28 : NEURON_GAP;
  layout = layoutBrain(brain, new Map(graphData.nodes.map((node) => [node.id, node])), gap);
  makeRegionLabels();
  const previous = new Map(nodes.map((n) => [n.id, n]));
  nodes = brain.neurons.map((neuron) => {
    // A refresh of the same target starts from where things were: they glide to their new place.
    const home = layout!.positions.get(neuron.id)!;
    const old = sameTarget ? previous.get(neuron.id) : undefined;
    return { id: neuron.id, neuron, heat: 0, home, x: old?.x ?? home.x, y: old?.y ?? home.y, z: old?.z ?? home.z };
  });
  nodeById.clear();
  nodes.forEach((n) => nodeById.set(n.id, n));
  links = brain.synapses.map((synapse) => ({ id: synapse.id, source: synapse.source, target: synapse.target, synapse, heat: 0, via: [] }));
  callLinks.clear();
  spawnLinks.clear();
  for (const link of links) {
    if (link.synapse.kind !== "state") {
      callLinks.set(`${link.synapse.source}>${link.synapse.target}`, link);
    }
  }
  pathway = new Set();
  if (!sameTarget) {
    fitted = false;
  }
  document.body.dataset.state = "busy";
  placeDebugMarks();
  graph.graphData({ nodes, links });
  messageEl.hidden = nodes.length > 0;
  if (!nodes.length) {
    showMessage(`No Python functions found in ${graphData.root}.`);
  }
  setStatus(summary());
}

function summary(): string {
  const calls = links.filter((l) => l.synapse.kind === "call").length;
  const state = links.length - calls;
  return `${nodes.length} functions · ${calls} call links · ${state} shared-state links`;
}

// Debugger

/** Finds the functions holding the breakpoints and the debugger's stop, in the current graph. */
function placeDebugMarks(): void {
  const neurons = brain?.neurons ?? [];
  breakpointNodes = new Set(breakpoints.flatMap(({ file, line }) => neuronAt(neurons, file, line)?.id ?? []));
  stoppedIn = debugStop && neuronAt(neurons, debugStop.file, debugStop.line)?.id;
}

function onDebugStopped(at: SourceLine | undefined): void {
  const wasStopped = stoppedIn !== undefined;
  debugStop = at;
  placeDebugMarks();
  const node = stoppedIn ? nodeById.get(stoppedIn) : undefined;
  if (node) {
    setStatus(`debugger stopped in ${node.neuron.label}, line ${at!.line}`);
    flyTo(node);
  } else if (wasStopped) {
    setStatus(live ? "live" : summary());
  }
}

// Live mode

function onLive(frame: LiveFrame): void {
  if (frame.done) {
    live = false;
    onStack = new Set();
    waiting = new Set();
    pathway = new Set();
    liveButton.textContent = "Run live";
    setStatus(`${summary()} · run finished`);
    pathEl.textContent = "";
    return;
  }
  live = true;
  liveButton.textContent = "Run again";
  const threads: ThreadStack[] = (frame.stacks ?? [])
    .map(({ thread, stack }) => ({ thread, stack: stack.filter((id) => nodeById.has(id)) }))
    .filter(({ stack }) => stack.length);
  const stacks = threads.map(({ stack }) => stack);
  onStack = new Set(stacks.flat());
  waiting = new Set(Object.keys(frame.waiting ?? {}).filter((id) => nodeById.has(id)));
  pathway = new Set();
  for (const stack of stacks) {
    for (let i = 1; i < stack.length; i++) {
      const link = callLinks.get(`${stack[i - 1]}>${stack[i]}`);
      if (link) {
        pathway.add(link);
      }
    }
  }
  let calls = 0;
  for (const [id, count] of Object.entries(frame.calls ?? {})) {
    const node = nodeById.get(id);
    if (node) {
      node.heat = 1;
      visited.add(id);
      calls += count;
    }
  }
  const pulses: [SynapseLink, number][] = [];
  let added = false;
  const use = (counts: Record<string, number> | undefined, known: Map<string, SynapseLink>, kind: SynapseKind) => {
    for (const [key, count] of Object.entries(counts ?? {})) {
      let link = known.get(key);
      if (!link) {
        const [source, target] = key.split(">");
        if (!nodeById.has(source) || !nodeById.has(target)) {
          continue;
        }
        // Not in the static graph (a call it missed, or a spawn): add a synapse from now on.
        const synapse: Synapse = { id: synapseId(kind, source, target), kind, source, target, variables: [], count: 0 };
        link = { id: synapse.id, source, target, synapse, heat: 1, via: [] };
        links.push(link);
        known.set(key, link);
        added = true;
      }
      link.heat = 1;
      visited.add(link.id);
      link.synapse.count += count;
      pulses.push([link, count]);
    }
  };
  use(frame.links, callLinks, "runtime");
  use(frame.spawns, spawnLinks, "spawn");
  if (added) {
    graph.graphData({ nodes, links }); // new link objects: pulses start with the next frame
  } else {
    for (const [link, count] of pulses) {
      for (let i = 0; i < Math.min(count, MAX_PULSES_PER_LINK); i++) {
        graph.emitParticle(link);
      }
    }
  }
  const now = performance.now();
  callsThisSecond = callsThisSecond.filter((c) => now - c.t < 1000);
  callsThisSecond.push({ t: now, n: calls });
  const rate = callsThisSecond.reduce((sum, c) => sum + c.n, 0);
  setStatus(`live · ${rate} calls/s`);
  pathEl.textContent = pathText(threads, frame.waiting ?? {});
}

/** One line per thread with what it runs, then what is waiting on an await. */
function pathText(threads: ThreadStack[], waitingCounts: Record<string, number>): string {
  const name = (id: string) => nodeById.get(id)!.neuron.label;
  const lines = threads.map(({ thread, stack }) => {
    const path = stack.map(name).join("  →  ");
    return threads.length > 1 || thread !== "MainThread" ? `${thread}:  ${path}` : path;
  });
  const waits = Object.entries(waitingCounts)
    .filter(([id]) => nodeById.has(id))
    .map(([id, count]) => (count > 1 ? `${name(id)} ×${count}` : name(id)));
  if (waits.length) {
    lines.push(`waiting:  ${waits.join(", ")}`);
  }
  if (lines.length > MAX_PATH_LINES) {
    lines.splice(MAX_PATH_LINES - 1, lines.length, `… and ${lines.length - MAX_PATH_LINES + 1} more`);
  }
  return lines.join("\n");
}

// UI

function reveal(node: NeuronNode): void {
  const loc = node.neuron.node.loc;
  vscode.postMessage({ type: "reveal", file: node.neuron.node.file, line: loc.line, col: loc.col });
  flyTo(node);
}

/**
 * Frames the whole brain from where the camera looks: its extent across and up the screen, so
 * the flat map fills the view, and room for labels.
 */
function fit(duration: number): void {
  if (!nodes.length) {
    return;
  }
  const camera = graph.camera() as THREE.PerspectiveCamera;
  const forward = camera.getWorldDirection(new THREE.Vector3());
  const right = new THREE.Vector3().crossVectors(forward, camera.up).normalize();
  const up = new THREE.Vector3().crossVectors(right, forward).normalize();
  const point = new THREE.Vector3();
  const low = [Infinity, Infinity, Infinity];
  const high = [-Infinity, -Infinity, -Infinity];
  for (const node of nodes) {
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
  const margin = nodes.length <= ALWAYS_LABEL ? 1.3 : 1.15;
  const vertical = (camera.fov / 2) * (Math.PI / 180);
  const horizontal = Math.atan(Math.tan(vertical) * camera.aspect);
  const halfWidth = Math.max((high[0] - low[0]) / 2, 20) * margin;
  const halfHeight = Math.max((high[1] - low[1]) / 2, 20) * margin;
  const distance = Math.max(halfWidth / Math.tan(horizontal), halfHeight / Math.tan(vertical)) + (high[2] - low[2]) / 2;
  const position = center.clone().addScaledVector(forward, -distance);
  graph.cameraPosition({ x: position.x, y: position.y, z: position.z }, { x: center.x, y: center.y, z: center.z }, duration);
}

function flyTo(node: NeuronNode): void {
  const distance = 80;
  const length = Math.hypot(node.x ?? 0, node.y ?? 0, node.z ?? 0) || 1;
  const ratio = 1 + distance / length;
  graph.cameraPosition(
    { x: (node.x ?? 0) * ratio, y: (node.y ?? 0) * ratio, z: (node.z ?? 0) * ratio },
    { x: node.x ?? 0, y: node.y ?? 0, z: node.z ?? 0 },
    800,
  );
}

function setStatus(text: string): void {
  statusEl.textContent = text;
}

function showMessage(text: string): void {
  messageEl.textContent = text;
  messageEl.hidden = false;
}

function resize(): void {
  const element = document.getElementById("brain")!;
  graph.width(element.clientWidth).height(element.clientHeight);
}
new ResizeObserver(resize).observe(document.getElementById("brain")!);

liveButton.addEventListener("click", () => vscode.postMessage({ type: "runLive" }));
document.getElementById("runWith")!.addEventListener("click", () => vscode.postMessage({ type: "runLiveWith" }));
stopButton.addEventListener("click", () => vscode.postMessage({ type: "stopLive" }));
document.getElementById("refresh")!.addEventListener("click", () => vscode.postMessage({ type: "refresh" }));
document.getElementById("fit")!.addEventListener("click", () => fit(600));

window.addEventListener("message", (event: MessageEvent<ToWebview>) => {
  const message = event.data;
  if (message.type === "loading") {
    setStatus(`Analyzing ${message.target}…`);
  } else if (message.type === "graph") {
    load(message.graph, message.target);
  } else if (message.type === "error") {
    showMessage(message.message);
    setStatus("");
  } else if (message.type === "liveStarted") {
    visited.clear();
    setStatus(`starting ${message.script}…`);
  } else if (message.type === "live") {
    onLive(message.frame);
  } else if (message.type === "breakpoints") {
    breakpoints = message.breakpoints;
    placeDebugMarks();
  } else if (message.type === "debugStopped") {
    onDebugStopped(message.at);
  } else if (message.type === "liveState") {
    const { running, stopping } = message.state;
    stopButton.hidden = !running || message.state.debugged; // the debug toolbar stops a debugged run
    stopButton.textContent = stopping ? "Force stop" : "Stop";
    if (running && stopping) {
      setStatus("stopping…");
    }
  }
});

// Hook for the browser harness in dev/ and for poking around from the webview devtools.
Object.assign(window, { luciole: { graph, live: onLive, isLive: () => live } });

vscode.postMessage({ type: "ready" });
