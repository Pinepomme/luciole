/**
 * Webview entry point: the 3D brain. Neurons are functions, synapses the variables flowing
 * between them. In live mode, functions light up as they run and each call traces its line from
 * the caller to the callee. Calm by design: neutral greys at rest, one accent color for activity
 * (the theme's), red only for the debugger.
 */

import ForceGraph3D from "3d-force-graph";
import * as THREE from "three";
import SpriteText from "three-spritetext";
import type { Graph } from "../graph";
import type { FromWebview, LiveFrame, SourceLine, ThreadStack, ToWebview } from "../messages";
import { Brain, breakpointAction, breakpointHint, buildBrain, Neuron, neuronAt, Synapse, SynapseKind, synapseId } from "./brain";
import { addCall, alongPolyline, partOf, Trace, traceAt } from "./flow";
import { HIDDEN, HIDDEN_LINE, LineBatch, PICK_SPHERE, SphereBatch } from "./batches";
import { bundle, labelAnchor, Layout, layoutBrain, NEURON_GAP, neuronRadius, Region, regionMembers, route, Vec3 } from "./layout";

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
  /** What its look last depended on (see animate): a settled neuron is skipped. */
  look?: number;
  /** Drawn by the sphere batch, in `color`. */
  parts?: { group: THREE.Group; color: THREE.Color; base: THREE.Color; ring?: THREE.Sprite; mark?: THREE.Sprite; label?: SpriteText; radius: number };
}

interface SynapseLink {
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

/** Frames with nothing changing before the brain stops being drawn. */
const STILL_FRAMES = 20;
/** Labels are always shown below this many neurons; above, only on lit or hovered ones. */
const ALWAYS_LABEL = 60;
/** Seconds for a flash to fade to about a third. */
const FADE = 0.35;
/** Share of their usual opacity kept by lines between regions. */
const INTER_REGION_OPACITY = 0.3;
/** Lines keep their full opacity up to this many; past it, all fade so that density, not brightness, shows tracts. */
const LINES_AT_FULL = 150;
/** Drawn spheres, as a share of the room the map gives each neuron. */
const SPHERE_SCALE = 0.5;
/** The ring around a lit function: its width, as a multiple of its sphere's radius, and never narrower on screen than RING_MIN_PX. */
const RING_SCALE = 2.6;
const RING_MIN_PX = 12;
/** Function names keep this height on screen, in pixels. */
const FUNCTION_LABEL_PX = 11;
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
const noticeEl = document.getElementById("notice")!;
document.getElementById("installDebugger")!.addEventListener("click", () => vscode.postMessage({ type: "installDebugger" }));
const liveButton = document.getElementById("live") as HTMLButtonElement;
const stopButton = document.getElementById("stop") as HTMLButtonElement;

const css = getComputedStyle(document.body);
const cssColor = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
const background = cssColor("--vscode-editor-background", "#1e1e1e");
const foreground = cssColor("--vscode-editor-foreground", "#cccccc");
const BACKGROUND = new THREE.Color(background);
const FOREGROUND = new THREE.Color(foreground);
/** A grey between the background and the text: 0 is the background, 1 the text. */
const grey = (share: number) => BACKGROUND.clone().lerp(FOREGROUND, share);
/** Light themes: thin grey strokes and text fade into white, so they need a darker grey. */
const LIGHT_THEME = BACKGROUND.getHSL({ h: 0, s: 0, l: 0 }).l > 0.5;
/** Text that names things, quieter than the code. */
const SECONDARY = `#${grey(LIGHT_THEME ? 0.75 : 0.62).getHexString()}`;
const PRIMARY = `#${grey(0.92).getHexString()}`;
/** Neurons at rest, and lines. */
const NEUTRAL = grey(LIGHT_THEME ? 0.45 : 0.4);
const LINE = grey(LIGHT_THEME ? 0.8 : 0.55);
/**
 * Each file's neurons get a muted tint, so you see at a glance which module a function is in:
 * Apple's system hues (no red: breakpoints and stops), half-way to the neutral grey so they stay
 * quiet next to the accent. Picked by a hash of the module, so a module keeps its tint.
 */
const MODULE_HUES = ["#30B0C7", "#34C759", "#5856D6", "#AF52DE", "#FF9F0A", "#00C7BE", "#7D7AFF", "#A2845E", "#64D2FF", "#FFD60A"];
const MODULE_TINT = LIGHT_THEME ? 0.55 : 0.5;
function moduleTint(module: string): THREE.Color {
  let hash = 2166136261;
  for (let i = 0; i < module.length; i++) {
    hash = Math.imul(hash ^ module.charCodeAt(i), 16777619);
  }
  return NEUTRAL.clone().lerp(new THREE.Color(MODULE_HUES[(hash >>> 0) % MODULE_HUES.length]), MODULE_TINT);
}
/** Activity (running, a call, the call path, waiting): the theme's accent. */
const ACCENT = new THREE.Color(themeAccent());
/** Where the debugger stopped, and breakpoints (VS Code's breakpoint red). */
const RED = new THREE.Color(cssColor("--vscode-debugIcon-breakpointForeground", "#e51400"));
/** The ring around what the Map view points at: the text color, not the accent (it is not activity). */
const FOCUS = new THREE.Color(PRIMARY);
const ringTexture = makeRingTexture();
const dotTexture = makeDotTexture();

/**
 * The theme's accent: the color of links, made to read on the editor background (focus borders
 * and buttons are greyish or dark in some themes), else the focus border, else the button color,
 * else system blue. Skips colors that are transparent or nearly grey.
 */
function themeAccent(): string {
  for (const name of ["--vscode-textLink-foreground", "--vscode-focusBorder", "--vscode-button-background"]) {
    const value = css.getPropertyValue(name).trim();
    const hex = /^#([0-9a-f]{6})([0-9a-f]{2})?$/i.exec(value);
    if (!hex || (hex[2] && parseInt(hex[2], 16) < 0x80)) {
      continue;
    }
    const hsl = new THREE.Color(`#${hex[1]}`).getHSL({ h: 0, s: 0, l: 0 });
    if (hsl.s > 0.35 && hsl.l > 0.2 && hsl.l < 0.8) {
      return `#${hex[1]}`;
    }
  }
  return "#0a84ff";
}

let brain: Brain | undefined;
let layout: Layout | undefined;
/** The accent strokes of calls being traced along their synapses. */
const flows = new THREE.Group();
/** Package, module and class names, above their regions. */
const regionLabels = new THREE.Group();
let target: string | undefined;
let nodes: NeuronNode[] = [];
let links: SynapseLink[] = [];
const nodeById = new Map<string, NeuronNode>();
/** Synapses that trace live calls (call or runtime) and spawns, by `a>b`. */
const callLinks = new Map<string, SynapseLink>();
const spawnLinks = new Map<string, SynapseLink>();
let hovered: NeuronNode | undefined;
let onStack = new Set<string>();
let waiting = new Set<string>();
let pathway = new Set<SynapseLink>();
/** Synapses with a call being traced on them. */
const flowing = new Set<SynapseLink>();
/** Synapses on the call path or fading from it: the only ones animate recolors. */
const warmLinks = new Set<SynapseLink>();
let live = false;
/** VS Code's breakpoints, and the functions that hold one. */
let breakpoints: SourceLine[] = [];
let breakpointNodes = new Set<string>();
/** Where the debugger stopped, and the function that holds it. */
let debugStop: SourceLine | undefined;
let stoppedIn: string | undefined;
/** What the Map view points at: a neuron, or a region whose name is then shown. */
let focused: string | undefined;
let focusedRegion: string | undefined;
/** The Map's focus changed: region names are placed again (the camera may not move). */
let refocused = false;
let callsThisSecond: { t: number; n: number }[] = [];

const graph = new ForceGraph3D(document.getElementById("brain")!)
  .backgroundColor(background)
  .showNavInfo(false)
  .nodeId("id")
  .nodeThreeObject((node: object) => neuronObject(node as NeuronNode))
  .nodePositionUpdate((object, { x, y, z }) => {
    // Matrices are updated by hand, when something moves (see neuronObject).
    const placed = (object.userData.placed ??= [NaN, NaN, NaN]) as number[];
    if (placed[0] !== x || placed[1] !== y || placed[2] !== z) {
      placed[0] = x;
      placed[1] = y;
      placed[2] = z;
      object.position.set(x, y, z);
      object.updateMatrix();
    }
    return true;
  })
  .nodeLabel((node: object) => neuronTooltip((node as NeuronNode).neuron))
  .linkThreeObject((link: object) => synapseObject(link as SynapseLink))
  .linkPositionUpdate((object, { start, end }, link) => {
    const synapseLink = link as SynapseLink;
    const ends = (synapseLink.ends ??= [NaN, NaN, NaN, NaN, NaN, NaN]);
    if (synapseLink.path && ends[0] === start.x && ends[1] === start.y && ends[2] === start.z && ends[3] === end.x && ends[4] === end.y && ends[5] === end.z) {
      return true; // its ends did not move (a drag moves one neuron, not the brain)
    }
    ends[0] = start.x;
    ends[1] = start.y;
    ends[2] = start.z;
    ends[3] = end.x;
    ends[4] = end.y;
    ends[5] = end.z;
    const points = bundle(start, synapseLink.via, end);
    const positions = (object as THREE.Line).geometry.getAttribute("position") as THREE.BufferAttribute;
    points.forEach((p, i) => positions.setXYZ(i, p.x, p.y, p.z));
    positions.needsUpdate = true;
    (object as THREE.Line).geometry.computeBoundingSphere();
    synapseLink.path = { points, ...alongPolyline(points) };
    lineBatch.move(synapseLink);
    return true;
  })
  .linkLabel((link: object) => synapseTooltip((link as SynapseLink).synapse))
  .cooldownTicks(120)
  .d3Force("charge", null)
  .d3Force("center", null)
  .d3Force("home", homeForce())
  .onEngineTick(() => {
    spheresMoved = true;
    if (away === 0) {
      // Everything is home: stop now, not after a countdown during which 3d-force-graph would
      // place every neuron and line again at each tick.
      graph.cooldownTime(0);
    }
  })
  .onNodeHover((node: object | null) => {
    hovered = (node as NeuronNode | null) ?? undefined;
    document.body.style.cursor = node ? "pointer" : "";
  })
  .onNodeClick((node: object) => {
    // Clicking another neuron moves on from what the Map pointed at.
    if ((node as NeuronNode).id !== focused) {
      focused = undefined;
    }
    focusedRegion = undefined;
    refocused = true;
    reveal(node as NeuronNode);
  })
  .onBackgroundClick(() => {
    focused = undefined;
    focusedRegion = undefined;
    refocused = true;
  })
  .onNodeRightClick((node: object) => {
    const { neuron } = node as NeuronNode;
    const action = breakpointAction(brain?.neurons ?? [], neuron, breakpoints);
    if (action && "add" in action) {
      vscode.postMessage({ type: "addBreakpoint", file: neuron.node.file, line: action.add });
    } else if (action) {
      vscode.postMessage({ type: "removeBreakpoints", file: neuron.node.file, lines: action.remove });
    }
  });

graph.scene().add(regionLabels, flows);
const sphereBatch = new SphereBatch(graph.scene());
const lineBatch = new LineBatch(graph.scene());
// Soft, even light from above and to the left: matte spheres, gently shaded.
const sun = new THREE.DirectionalLight(0xffffff, 0.4 * Math.PI);
sun.position.set(-0.5, 1, 0.8);
graph.lights([new THREE.AmbientLight(0xffffff, 0.65 * Math.PI), sun]);
// The map places neurons: links keep their force only to join their ends to the nodes (when it
// starts), and do no work at each tick.
(graph.d3Force("link") as unknown as { strength(k: number): { iterations(n: number): void } }).strength(0).iterations(0);

/** Neurons away from their place at the last tick: none, and the layout engine can stop. */
let away = 0;
/** How long the layout engine may run once started, when something is away (3d-force-graph's default). */
const ENGINE_TIME = 15000;

/** Runs the layout engine again (a drag, a new graph): it stops once every neuron is home. */
function startEngine(): void {
  graph.cooldownTime(ENGINE_TIME);
}

/** Pulls each neuron to its place on the map. They start there: this undoes drags and glides refreshes. */
function homeForce() {
  let pulled: NeuronNode[] = [];
  const force = (alpha: number) => {
    const k = 0.3 * Math.max(alpha, 0.05);
    away = 0;
    for (const node of pulled) {
      const n = node as NeuronNode & { vx: number; vy: number; vz: number };
      const [dx, dy, dz] = [node.home.x - node.x!, node.home.y - node.y!, node.home.z - node.z!];
      if (Math.abs(dx) + Math.abs(dy) + Math.abs(dz) < 0.05 && Math.abs(n.vx) + Math.abs(n.vy) + Math.abs(n.vz) < 0.05) {
        // Home: exactly, and still. Pulled closer forever, every neuron would move a little each
        // tick, and every line would be placed again.
        node.x = node.home.x;
        node.y = node.home.y;
        node.z = node.home.z;
        n.vx = n.vy = n.vz = 0;
        continue;
      }
      away++;
      n.vx += dx * k;
      n.vy += dy * k;
      n.vz += dz * k;
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

/**
 * Bumped when the pointer moves, the camera comes to rest or the graph's objects are made (a new
 * graph, synapses found at runtime): what the pointer is over may have changed.
 */
let rayVersion = 0;

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

function neuronObject(node: NeuronNode): THREE.Object3D {
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
function pickableWhenShown(sprite: THREE.Sprite): void {
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
function ringOf(parts: NonNullable<NeuronNode["parts"]>): THREE.Sprite {
  return (parts.ring ??= sprite(parts, new THREE.SpriteMaterial({ map: ringTexture, color: ACCENT, transparent: true, opacity: 0, depthWrite: false })));
}

/** A red dot on functions that hold a breakpoint, as in the editor's gutter. */
function markOf(parts: NonNullable<NeuronNode["parts"]>): THREE.Sprite {
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

function synapseObject(link: SynapseLink): THREE.Object3D {
  rayVersion++; // see neuronObject
  const geometry = new THREE.BufferGeometry();
  link.via = layout ? route(layout, link.synapse.source, link.synapse.target) : [];
  const count = bundle({ x: 0, y: 0, z: 0 }, link.via, { x: 0, y: 0, z: 0 }).length;
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(count * 3), 3));
  link.points = count;
  link.color = LINE.clone();
  link.opacity = baseOpacity(link);
  link.line = new THREE.Line(geometry, HIDDEN_LINE); // for hover; the line batch draws it
  link.line.matrixAutoUpdate = false; // its points are where it is
  return link.line;
}

/**
 * Lines between regions are fainter: they add up into tracts where many travel together. Big
 * brains have fainter lines all over.
 */
function baseOpacity(link: SynapseLink): number {
  const opacity = { call: 0.5, state: 0.25, runtime: 0.3, spawn: 0.4 }[link.synapse.kind] * Math.max(0.4, Math.min(1, Math.sqrt(LINES_AT_FULL / Math.max(links.length, 1))));
  return link.via.length ? opacity * INTER_REGION_OPACITY : opacity;
}

/** The system's interface font, as on macOS, then VS Code's. */
const FONT = `-apple-system, BlinkMacSystemFont, "SF Pro Text", ${css.getPropertyValue("--vscode-font-family").trim() || "system-ui"}, sans-serif`;

/** A label per region (package, module, class), sized by the region; declutterLabels places them. */
function makeRegionLabels(): void {
  for (const child of regionLabels.children) {
    (child as SpriteText).material.map?.dispose();
    (child as SpriteText).material.dispose();
  }
  regionLabels.clear();
  const members = layout ? regionMembers(layout) : new Map<string, string[]>();
  for (const region of layout?.regions.values() ?? []) {
    // A region holding the whole brain (a single file) needs no name: it is the brain.
    const whole = region.size === layout!.regions.get("")!.size;
    if (region.kind === "root" || whole || (region.kind === "class" && region.size < 3)) {
      continue;
    }
    const text = new SpriteText(region.label, 10, SECONDARY); // resized each frame to a fixed height on screen
    text.fontFace = FONT;
    text.fontWeight = region.kind === "class" ? "normal" : "500";
    text.material.depthTest = false;
    text.material.depthWrite = false;
    text.material.opacity = region.kind === "class" ? 0.75 : 1;
    text.renderOrder = 1;
    text.userData = { region, members: members.get(region.id) ?? [], aspect: text.scale.x / text.scale.y };
    regionLabels.add(text);
  }
}

/** A thin circle, crisp at the sizes it is drawn. */
function makeRingTexture(): THREE.Texture {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 128;
  const context = canvas.getContext("2d")!;
  context.beginPath();
  context.arc(64, 64, 61, 0, 2 * Math.PI);
  context.lineWidth = 3.5;
  context.strokeStyle = "#ffffff";
  context.stroke();
  const texture = new THREE.CanvasTexture(canvas);
  texture.anisotropy = 4;
  return texture;
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
    const text = new SpriteText(node.neuron.label, 2.6, SECONDARY); // resized each frame to a fixed height on screen
    text.fontFace = FONT;
    text.fontWeight = "300";
    // Drawn over the spheres: a neighbour in front no longer hides it (declutterLabels hides
    // labels that overlap a more important one instead).
    text.material.depthTest = false;
    text.material.depthWrite = false;
    text.renderOrder = 1;
    text.userData = { aspect: text.scale.x / text.scale.y };
    pickableWhenShown(text); // a click on a function's name opens it, as on its sphere
    text.matrixAutoUpdate = false; // updated where it is placed (declutterLabels)
    parts.group.add(text);
    parts.label = text;
  }
  return parts.label;
}

/** Which label stays when two overlap on screen: the hovered one, the Map's, where the debugger stopped, what runs. */
function labelPriority(node: NeuronNode): number {
  const rank =
    node === hovered ? 6 : node.id === focused ? 5 : node.id === stoppedIn ? 4 : onStack.has(node.id) ? 3 : node.heat > 0.15 || waiting.has(node.id) ? 2 : breakpointNodes.has(node.id) ? 1 : 0;
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
  const perspective = camera as THREE.PerspectiveCamera;
  const fovScale = 2 * Math.tan(THREE.MathUtils.degToRad(perspective.fov) / 2);
  const shown: { text: SpriteText; priority: number }[] = [];
  for (const node of nodes) {
    const parts = node.parts;
    if (parts?.label) {
      // Hidden last time for overlapping another: maybe not any more.
      parts.label.visible = parts.label.userData.wanted === true;
    }
    if (parts?.label?.visible) {
      // One height on screen, just above the sphere, its ring and its breakpoint dot.
      const text = parts.label;
      const unit = (Math.max(1, camera.position.distanceTo(center.set(node.x ?? 0, node.y ?? 0, node.z ?? 0))) * fovScale) / height;
      const tall = FUNCTION_LABEL_PX * unit;
      text.scale.set(tall * text.userData.aspect, tall, 1);
      const top = Math.max(
        parts.radius,
        // Room for the breakpoint dot (markOf) whether or not there is one: the name does not jump
        // when one is added, and stays clear of a close sphere, which looks bigger than its radius.
        parts.radius * 0.9 + Math.max(2, parts.radius * 0.9) / 2,
        parts.ring?.visible ? parts.ring.scale.y / 2 : 0,
      );
      text.position.y = top + 3 * unit + tall / 2;
      text.updateMatrix();
      shown.push({ text, priority: labelPriority(node) });
    }
  }
  for (const child of regionLabels.children) {
    const text = child as SpriteText;
    const region = text.userData.region as Region;
    // Pixels per world unit at the region's distance: names keep one height on screen, and show
    // while the region is big enough to be worth naming but doesn't fill the view.
    const distance = Math.max(1, camera.position.distanceTo(center.set(region.center.x, region.center.y, region.center.z)));
    const pixels = height / (distance * fovScale);
    const span = 2 * region.radius * pixels;
    // The region the Map points at is always named, in the text color.
    const isFocused = region.id === focusedRegion;
    text.visible = isFocused || (span >= REGION_NAMED_MIN && span <= REGION_NAMED_MAX * Math.max(width, height));
    if (!text.visible) {
      continue;
    }
    const color = isFocused ? PRIMARY : SECONDARY;
    if (text.color !== color) {
      text.color = color;
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
      shown.push({ text, priority: isFocused ? 9000 : 900 - region.depth * 100 + Math.min(region.radius, 999) / 10 });
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
/** A neuron moved since the last frame (the layout engine ticked): spheres and labels follow. */
let spheresMoved = true;
/** The view was resized: labels keep one height on screen, so they are placed again. */
let resized = false;
/** Frames in a row where nothing changed; past STILL_FRAMES, the brain stops being drawn. */
let stillFrames = 0;
let paused = false;
/** Keep drawing until then (a camera flight, input just now). */
let awakeUntil = 0;
const lastCamera = new THREE.Matrix4();
let cameraWasMoving = false;
let ancestorsStill = false;
/** How many synapses the lines' opacity was set for. */
let styledLinks = -1;

function animate(now: number): void {
  const dt = Math.min((now - last) / 1000, 0.1);
  last = now;
  if (paused) {
    // Still: whatever changes it (input, a message, a resize) wakes it first.
    requestAnimationFrame(animate);
    return;
  }
  const fade = Math.exp(-dt / FADE);
  const showAll = nodes.length <= ALWAYS_LABEL;
  let recolored = false;
  let relabeled = false;
  let warm = 0;
  for (const node of nodes) {
    const isStopped = node.id === stoppedIn;
    node.heat = onStack.has(node.id) || isStopped ? 1 : node.heat * fade < 0.002 ? 0 : node.heat * fade;
    const parts = node.parts;
    if (!parts) {
      continue;
    }
    // Waiting on an await: a ring and no fill, unless the function is also running or just ran.
    const isWaiting = waiting.has(node.id) && node.heat < 0.5 && !isStopped;
    const hasBreakpoint = breakpointNodes.has(node.id);
    // What the Map points at gets a ring in the text color, while nothing lights it up.
    const isFocused = node.id === focused;
    // A cold neuron whose look depends on nothing that changed is left as it is: most of them.
    const look = (isStopped ? 1 : 0) | (isWaiting ? 2 : 0) | (node === hovered ? 4 : 0) | (hasBreakpoint ? 8 : 0) | (showAll ? 16 : 0) | (isFocused ? 32 : 0);
    // A steady ring (waiting on an await, or the Map's) is only scaled again when the camera moved:
    // else a coroutine waiting for minutes, or a function left focused, would keep the brain drawing.
    const steadyRing = (isWaiting || isFocused) && !cameraWasMoving;
    if (node.heat === 0 && node.look === look && (!parts.ring?.visible || steadyRing)) {
      continue;
    }
    node.look = look;
    warm += node.heat > 0 ? 1 : 0;
    recolored = true;
    relabeled = true;
    if (hasBreakpoint || parts.mark) {
      markOf(parts).visible = hasBreakpoint; // a sprite drawn at opacity 0 still costs a draw call
    }
    const color = isStopped ? RED : ACCENT;
    parts.color.copy(parts.base).lerp(color, node.heat);
    let ringColor = color;
    let ringOpacity = isWaiting ? 1 : node.heat;
    if (isFocused && ringOpacity < 0.5) {
      ringColor = FOCUS;
      ringOpacity = 1;
    }
    if (ringOpacity > 0 || parts.ring) {
      const ring = ringOf(parts);
      const material = ring.material as THREE.SpriteMaterial;
      material.color.copy(ringColor);
      material.opacity = ringOpacity;
      ring.visible = ringOpacity > 0;
      if (ring.visible) {
        ring.scale.setScalar(Math.max(parts.radius * RING_SCALE, RING_MIN_PX * worldPerPixel(ring)));
        ring.updateMatrix();
      }
    }
    const wantsLabel = showAll || node.heat > 0.15 || isWaiting || isFocused || node === hovered || hasBreakpoint;
    if (wantsLabel || parts.label) {
      const text = label(node);
      text.userData.wanted = wantsLabel; // declutterLabels starts from it: a cold neuron's is not set again
      text.visible = wantsLabel;
      // What runs, waits, stopped or is pointed at is named in the text color, the rest in grey.
      const color = node.heat > 0.15 || isWaiting || isStopped || isFocused || node === hovered ? PRIMARY : SECONDARY;
      if (wantsLabel && text.color !== color) {
        text.color = color;
      }
    }
  }
  if (!ancestorsStill) {
    // The groups holding the neurons (3d-force-graph's, the scene) never move: an object that
    // updates its matrix makes three.js recompute all the matrices under it, every frame.
    const holder = nodes.find((node) => node.parts)?.parts!.group.parent;
    for (let object = holder; object; object = object.parent) {
      object.updateMatrix();
      object.matrixAutoUpdate = false;
      ancestorsStill = true;
    }
  }
  const camera = graph.camera();
  // The controls' damping eases the camera to a stop without ever quite stopping: moves too small
  // to see don't count.
  const cameraMoved = camera.matrixWorld.elements.some((v, i) => Math.abs(v - lastCamera.elements[i]) > 1e-4 * (1 + Math.abs(v)));
  if (cameraMoved) {
    lastCamera.copy(camera.matrixWorld);
  } else if (cameraWasMoving) {
    rayVersion++; // the camera came to rest: what is under the pointer now (not during the turn)
  }
  cameraWasMoving = cameraMoved;
  // Placing labels compares them on screen: only when the camera, the view, a neuron, a label or the Map's focus changed.
  if (cameraMoved || spheresMoved || relabeled || resized || refocused) {
    resized = false;
    refocused = false;
    declutterLabels();
  }
  if (links.length !== styledLinks) {
    // Lines fade as there are more of them (baseOpacity): restyle them all when synapses are added.
    styledLinks = links.length;
    for (const link of links) {
      link.opacity = link.color ? baseOpacity(link) + (1 - baseOpacity(link)) * link.heat : link.opacity;
    }
    lineBatch.rewriteAll();
  }
  for (const link of warmLinks) {
    // The call path in the accent, like its functions; it eases back to grey once left.
    const onPath = pathway.has(link);
    link.heat = onPath ? 1 : link.heat * fade < 0.002 ? 0 : link.heat * fade;
    if (link.color) {
      link.color.copy(LINE).lerp(ACCENT, link.heat);
      link.opacity = baseOpacity(link) + (1 - baseOpacity(link)) * link.heat;
      lineBatch.recolor(link);
    }
    if (!onPath && link.heat === 0) {
      warmLinks.delete(link);
    }
  }
  sphereBatch.update(nodes, spheresMoved, recolored);
  lineBatch.update(links);
  flow(now / 1000);
  // Nothing moves, fades or flows: stop drawing frames (no GPU or CPU spent on a still brain)
  // until input, a message or a change wakes it.
  const busy = now < awakeUntil || cameraMoved || spheresMoved || recolored || warm > 0 || warmLinks.size > 0 || flowing.size > 0;
  spheresMoved = false;
  stillFrames = busy ? 0 : stillFrames + 1;
  if (busy && paused) {
    paused = false;
    graph.resumeAnimation();
  } else if (!busy && !paused && stillFrames > STILL_FRAMES) {
    paused = true;
    graph.pauseAnimation();
  }
  requestAnimationFrame(animate);
}

/** Draws again for at least `ms` (input, a message, a camera flight). */
function wake(ms = 500): void {
  awakeUntil = Math.max(awakeUntil, performance.now() + ms);
  stillFrames = 0;
  if (paused) {
    paused = false;
    graph.resumeAnimation();
  }
}

document.addEventListener("visibilitychange", () => wake()); // shown again: draw it again
for (const type of ["pointerdown", "pointermove", "pointerup", "wheel", "keydown"]) {
  document.getElementById("brain")!.addEventListener(
    type,
    (event) => {
      rayVersion++;
      if (event.type === "pointerdown") {
        startEngine(); // before a drag starts it
      }
      wake();
    },
    { capture: true, passive: true },
  );
}

const worldPoint = new THREE.Vector3();
/** World units per screen pixel at an object's distance from the camera. */
function worldPerPixel(object: THREE.Object3D): number {
  const camera = graph.camera() as THREE.PerspectiveCamera;
  const distance = camera.position.distanceTo(object.getWorldPosition(worldPoint));
  const height = document.getElementById("brain")!.clientHeight || 1;
  return (distance * 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2)) / height;
}

/** Draws each traced call: the accent stroke over its line, from the caller to where the sweep is. */
function flow(seconds: number): void {
  for (const link of flowing) {
    const state = link.trace && traceAt(link.trace, seconds);
    if (!state) {
      disposeStroke(link);
      link.trace = undefined;
      flowing.delete(link);
      continue;
    }
    if (!link.path) {
      continue; // a synapse found at runtime: drawn from the next frame
    }
    const { points, along } = link.path;
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
      flows.add(link.stroke);
    }
    const drawn = partOf(points, along, state.head);
    const positions = link.stroke.geometry.getAttribute("position") as THREE.BufferAttribute;
    drawn.forEach((p, i) => positions.setXYZ(i, p.x, p.y, p.z));
    positions.needsUpdate = true;
    link.stroke.geometry.setDrawRange(0, drawn.length);
    link.stroke.material.opacity = state.opacity;
  }
}

function disposeStroke(link: SynapseLink): void {
  if (link.stroke) {
    link.stroke.removeFromParent();
    link.stroke.geometry.dispose();
    link.stroke.material.dispose();
    link.stroke = undefined;
  }
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
  flowing.forEach(disposeStroke);
  flowing.clear();
  warmLinks.clear();
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
    focused = undefined;
    focusedRegion = undefined;
  }
  document.body.dataset.state = "busy";
  placeDebugMarks();
  setGraph();
  messageEl.hidden = nodes.length > 0;
  if (!nodes.length) {
    showMessage(`No Python functions found in ${graphData.root}.`);
  }
  setStatus(summary());
}

/** Hands nodes and links to 3d-force-graph, which places them (its layout engine) and makes their objects. */
function setGraph(): void {
  startEngine();
  graph.graphData({ nodes, links }); // their objects are made a moment later (neuronObject, synapseObject)
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
        warmLinks.add(link);
      }
    }
  }
  let calls = 0;
  for (const [id, count] of Object.entries(frame.calls ?? {})) {
    const node = nodeById.get(id);
    if (node) {
      node.heat = 1;
      calls += count;
    }
  }
  const seconds = performance.now() / 1000;
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
        link = { id: synapse.id, source, target, synapse, heat: 0, via: [] };
        links.push(link);
        known.set(key, link);
        added = true;
      }
      link.synapse.count += count;
      // A new synapse has no path yet: its first sweep takes a medium length.
      link.trace = addCall(link.trace, seconds, link.path?.length ?? 150);
      flowing.add(link);
    }
  };
  use(frame.links, callLinks, "runtime");
  use(frame.spawns, spawnLinks, "spawn");
  if (added) {
    setGraph(); // their lines, and their traced calls, appear with the next frame
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

/** The Map view points at a neuron: fly to it and ring it. */
function focusNode(id: string): void {
  const node = nodeById.get(id);
  if (node) {
    focused = id;
    focusedRegion = undefined;
    refocused = true;
    flyTo(node);
  }
}

/** The Map view points at a region: frame its neurons and name it. */
function focusRegion(id: string): void {
  const members = layout ? (regionMembers(layout).get(id) ?? []) : [];
  const shown = members.map((member) => nodeById.get(member)).filter((node): node is NeuronNode => node !== undefined);
  if (shown.length) {
    focused = undefined;
    focusedRegion = id;
    refocused = true;
    fit(800, shown);
  }
}

/**
 * Frames the whole brain (or `subset` of it) from where the camera looks: its extent across and
 * up the screen, so the flat map fills the view, and room for labels.
 */
function fit(duration: number, subset: NeuronNode[] = nodes): void {
  if (!subset.length) {
    return;
  }
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
  const margin = nodes.length <= ALWAYS_LABEL ? 1.3 : 1.15;
  const vertical = (camera.fov / 2) * (Math.PI / 180);
  const horizontal = Math.atan(Math.tan(vertical) * camera.aspect);
  const halfWidth = Math.max((high[0] - low[0]) / 2, 20) * margin;
  const halfHeight = Math.max((high[1] - low[1]) / 2, 20) * margin;
  const distance = Math.max(halfWidth / Math.tan(horizontal), halfHeight / Math.tan(vertical)) + (high[2] - low[2]) / 2;
  const position = center.clone().addScaledVector(forward, -distance);
  wake(duration + 300);
  graph.cameraPosition({ x: position.x, y: position.y, z: position.z }, { x: center.x, y: center.y, z: center.z }, duration);
}

function flyTo(node: NeuronNode): void {
  const distance = 80;
  const length = Math.hypot(node.x ?? 0, node.y ?? 0, node.z ?? 0) || 1;
  const ratio = 1 + distance / length;
  wake(1100);
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
  resized = true;
  wake();
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
  wake();
  if (message.type === "loading") {
    setStatus(`Analyzing ${message.target}…`);
  } else if (message.type === "graph") {
    load(message.graph, message.target);
  } else if (message.type === "error") {
    showMessage(message.message);
    setStatus("");
  } else if (message.type === "liveStarted") {
    setStatus(`starting ${message.script}…`);
  } else if (message.type === "live") {
    onLive(message.frame);
  } else if (message.type === "breakpoints") {
    breakpoints = message.breakpoints;
    placeDebugMarks();
  } else if (message.type === "debugStopped") {
    onDebugStopped(message.at);
  } else if (message.type === "debuggerNotice") {
    noticeEl.hidden = !message.show;
  } else if (message.type === "focus") {
    focusNode(message.node);
  } else if (message.type === "focusRegion") {
    focusRegion(message.region);
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
Object.assign(window, {
  luciole: {
    graph,
    live: (frame: LiveFrame) => {
      wake(); // as a message would: a still brain is not drawn
      onLive(frame);
    },
    isLive: () => live,
    paused: () => paused,
  },
});

vscode.postMessage({ type: "ready" });
