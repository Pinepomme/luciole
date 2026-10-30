/**
 * Webview entry point: the 3D brain. Neurons are functions, synapses the variables flowing
 * between them. In live mode, functions light up as they run and each call traces its line from
 * the caller to the callee. Calm by design: neutral greys at rest, one accent color for activity
 * (the theme's), red only for the debugger.
 */

import ForceGraph3D from "3d-force-graph";
import * as THREE from "three";
import type { Graph } from "../graph";
import type { OriginPath } from "../origin";
import type { FromWebview, LiveFrame, SourceLine, ThreadStack, ToWebview } from "../messages";
import { Brain, breakpointAction, buildBrain, neuronAt, Synapse, SynapseKind, synapseId } from "./brain";
import { Filters, filtersText, NO_FILTERS, originShades, Shade, shades } from "./filters";
import { addCall, alongPolyline, traceAt } from "./flow";
import { LineBatch, SphereBatch } from "./batches";
import { fitCamera, flyToNode } from "./camera";
import { declutterLabels, label, makeRegionLabels, regionLabels } from "./labels";
import { bundle, homeForce, Layout, layoutBrain, layoutByCalls, NEURON_GAP, regionMembers, Vec3 } from "./layout";
import { baseOpacity, disposeStroke, drawStroke, markOf, NeuronNode, neuronObject, onRightClick, raysChanged, ringOf, SynapseLink, synapseObject, worldPerPixel } from "./objects";
import { OriginLight, routeBetween } from "./originlight";
import { ReplayBar } from "./replay";
import { neuronTooltip, pathText, synapseTooltip } from "./text";
import { ACCENT, background, FOCUS, LINE, PRIMARY, RED, SECONDARY } from "./theme";

declare function acquireVsCodeApi(): { postMessage(message: FromWebview): void };

/** Frames with nothing changing before the brain stops being drawn. */
const STILL_FRAMES = 20;
/** Labels are always shown below this many neurons; above, only on lit or hovered ones. */
const ALWAYS_LABEL = 60;
/** Seconds for a flash to fade to about a third. */
const FADE = 0.35;
/** The ring around a lit function: its width, as a multiple of its sphere's radius, and never narrower on screen than RING_MIN_PX. */
const RING_SCALE = 2.6;
const RING_MIN_PX = 12;
/** Filtered-out neurons fade this far into the background (dimmed: outside the focus or an origin path; hidden: tests, helpers), their lines to this share of their opacity. */
const SHADE_FADE = { dim: 0.95, hidden: 0.975 };
const SHADE_LINES = { dim: 0.06, hidden: 0.02 };
const BACKGROUND = new THREE.Color(background);

const vscode = acquireVsCodeApi();
const statusEl = document.getElementById("status")!;
const pathEl = document.getElementById("path")!;
const messageEl = document.getElementById("message")!;
const noticeEl = document.getElementById("notice")!;
document.getElementById("installDebugger")!.addEventListener("click", () => vscode.postMessage({ type: "installDebugger" }));
const liveButton = document.getElementById("live") as HTMLButtonElement;
const stopButton = document.getElementById("stop") as HTMLButtonElement;

let brain: Brain | undefined;
let layout: Layout | undefined;
/** The accent strokes of calls being traced along their synapses. */
const flows = new THREE.Group();
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
/** Focus and filters (filters.ts), and the neurons they shade. */
let filters: Filters = NO_FILTERS;
let shaded = new Map<string, Shade>();
const clearFiltersButton = document.getElementById("clearFilters") as HTMLButtonElement;

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
  .nodeLabel((node: object) => neuronTooltip((node as NeuronNode).neuron, brain?.neurons ?? [], breakpoints))
  .linkThreeObject((link: object) => synapseObject(link as SynapseLink, layout, links.length))
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
  .d3Force("home", homeForce((count) => (away = count)))
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
  });

graph.scene().add(regionLabels, flows);
const sphereBatch = new SphereBatch(graph.scene());
const lineBatch = new LineBatch(graph.scene());
/** "Where did this value come from?": its path lit on the brain (originlight.ts). */
const origin = new OriginLight(graph.scene());
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

let fitted = false;
graph.onEngineStop(() => {
  if (!fitted) {
    fitted = true;
    fit(600);
  }
  document.body.dataset.state = "ready";
});

/** Which label stays when two overlap on screen: the hovered one, the Map's, where the debugger stopped, what runs. */
function labelPriority(node: NeuronNode): number {
  const rank =
    node === hovered ? 6 : node.id === focused ? 5 : node.id === stoppedIn ? 4 : onStack.has(node.id) || origin.members.has(node.id) ? 3 : node.heat > 0.15 || waiting.has(node.id) ? 2 : breakpointNodes.has(node.id) ? 1 : 0;
  return rank * 1000 + node.parts!.radius; // then bigger (more connected) neurons first
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
    // What the Map points at (or where an origin was asked) gets a ring in the text color, while nothing lights it up.
    const isFocused = node.id === focused || node.id === origin.start;
    const onPath = origin.members.has(node.id);
    const shade = shaded.get(node.id);
    // A cold neuron whose look depends on nothing that changed is left as it is: most of them.
    const look = (isStopped ? 1 : 0) | (isWaiting ? 2 : 0) | (node === hovered ? 4 : 0) | (hasBreakpoint ? 8 : 0) | (showAll ? 16 : 0) | (isFocused ? 32 : 0) | (shade === "dim" ? 64 : shade ? 128 : 0) | (onPath ? 256 : 0);
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
    parts.color.copy(parts.base);
    if (shade) {
      parts.color.lerp(BACKGROUND, SHADE_FADE[shade]);
    }
    parts.color.lerp(color, node.heat);
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
        ring.scale.setScalar(Math.max(parts.radius * RING_SCALE, RING_MIN_PX * worldPerPixel(ring, graph.camera() as THREE.PerspectiveCamera)));
        ring.updateMatrix();
      }
    }
    const wantsLabel = (showAll && !shade) || node.heat > 0.15 || isWaiting || isFocused || onPath || node === hovered || hasBreakpoint;
    if (wantsLabel || parts.label) {
      const text = label(node);
      text.userData.wanted = wantsLabel; // declutterLabels starts from it: a cold neuron's is not set again
      text.visible = wantsLabel;
      // What runs, waits, stopped, is pointed at or on an origin path is named in the text color, the rest in grey.
      const color = node.heat > 0.15 || isWaiting || isStopped || isFocused || onPath || node === hovered ? PRIMARY : SECONDARY;
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
    raysChanged(); // the camera came to rest: what is under the pointer now (not during the turn)
  }
  cameraWasMoving = cameraMoved;
  // Placing labels compares them on screen: only when the camera, the view, a neuron, a label or the Map's focus changed.
  if (cameraMoved || spheresMoved || relabeled || resized || refocused) {
    resized = false;
    refocused = false;
    declutterLabels(camera, nodes, nodeById, focusedRegion, labelPriority);
  }
  if (links.length !== styledLinks) {
    // Lines fade as there are more of them (baseOpacity): restyle them all when synapses are added.
    styledLinks = links.length;
    for (const link of links) {
      link.opacity = link.color ? linkOpacity(link) : link.opacity;
    }
    lineBatch.rewriteAll();
  }
  for (const link of warmLinks) {
    // The call path in the accent, like its functions; it eases back to grey once left.
    const onPath = pathway.has(link);
    link.heat = onPath ? 1 : link.heat * fade < 0.002 ? 0 : link.heat * fade;
    if (link.color) {
      link.color.copy(LINE).lerp(ACCENT, link.heat);
      link.opacity = linkOpacity(link);
      lineBatch.recolor(link);
    }
    if (!onPath && link.heat === 0) {
      warmLinks.delete(link);
    }
  }
  sphereBatch.update(nodes, spheresMoved, recolored);
  lineBatch.update(links);
  flow(now / 1000);
  if (origin.path && spheresMoved) {
    origin.place(originRoute); // a neuron moved: the path's lines follow
  }
  const tracing = origin.draw(now / 1000, (id) => {
    const node = nodeById.get(id);
    if (node) {
      node.heat = 1; // a pulse: it fades as a call's light does
    }
  });
  // Nothing moves, fades or flows: stop drawing frames (no GPU or CPU spent on a still brain)
  // until input, a message or a change wakes it.
  const busy = now < awakeUntil || cameraMoved || spheresMoved || recolored || warm > 0 || warmLinks.size > 0 || flowing.size > 0 || tracing;
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

/** A line's opacity: fainter as there are more (baseOpacity), full on the call path, faint when an end is filtered out. */
function linkOpacity(link: SynapseLink): number {
  const base = baseOpacity(link, links.length);
  const ends = [link.synapse.source, link.synapse.target].map((id) => shaded.get(id));
  const shade = ends.includes("hidden") ? SHADE_LINES.hidden : ends.includes("dim") ? SHADE_LINES.dim : 1;
  return (base + (1 - base) * link.heat) * shade;
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
      raysChanged();
      if (event.type === "pointerdown") {
        startEngine(); // before a drag starts it
      }
      wake();
    },
    { capture: true, passive: true },
  );
}

/** Right-click adds or removes a neuron's breakpoints. */
onRightClick(document.getElementById("brain")!, graph, (node) => {
  const action = breakpointAction(brain?.neurons ?? [], node.neuron, breakpoints);
  if (action && "add" in action) {
    vscode.postMessage({ type: "addBreakpoint", file: node.neuron.node.file, line: action.add });
  } else if (action) {
    vscode.postMessage({ type: "removeBreakpoints", file: node.neuron.node.file, lines: action.remove });
  }
});

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
    drawStroke(link, link.path, state.head, state.opacity, flows);
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
  const graphNodes = new Map(graphData.nodes.map((node) => [node.id, node]));
  // Small brains read best as rows by call order; a map of regions helps only with many.
  layout = brain.neurons.length <= ALWAYS_LABEL ? layoutByCalls(brain, graphNodes, gap) : layoutBrain(brain, graphNodes, gap);
  makeRegionLabels(layout);
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
  shaded = shading();
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

/** Lights the brain with a frame of a live run, or of a replay (`replaying`: the replay bar says where it is). */
function onLive(frame: LiveFrame, replaying = false): void {
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
  if (!replaying) {
    liveButton.textContent = "Run again";
  }
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
  if (!replaying) {
    setStatus(`live · ${rate} calls/s`);
  }
  pathEl.textContent = pathText(threads, frame.waiting ?? {}, (id) => nodeById.get(id)?.neuron.label);
}

// Replay and filters

const replay = new ReplayBar({
  post: (message) => vscode.postMessage(message),
  show: (frame) => {
    wake();
    onLive(frame, true);
  },
  reset: () => {
    onLive({ done: true });
    setStatus(summary());
  },
  status: setStatus,
});

/** New focus or filters: neurons and lines are recolored (the look of each shaded one changed), no object added. */
function applyFilters(next: Filters): void {
  filters = next;
  shaded = shading();
  styledLinks = -1; // every line restyled with the next frame
  const text = filtersText(filters, (id) => nodeById.get(id)?.neuron.label);
  clearFiltersButton.hidden = !text;
  clearFiltersButton.title = text ? `Showing ${text}: click to show every function again` : "";
  setStatus([summary(), text, origin.path && `where ${origin.path.label} came from`].filter(Boolean).join(" · "));
  if (filters.around && nodeById.has(filters.around)) {
    // Ringed as the Map's choice is, and the functions around it framed.
    focused = filters.around;
    focusedRegion = undefined;
    refocused = true;
    fit(800, nodes.filter((node) => !shaded.has(node.id)));
  }
  wake();
}

/** The neurons focus and filters shade; with an origin path shown, every one off the path too. */
function shading(): Map<string, Shade> {
  return !brain ? new Map() : origin.path ? originShades(brain, filters, origin.members) : shades(brain, filters);
}

/** An origin path: its functions stand out, the value's moves trace their lines, the camera frames it. */
function showOrigin(path: OriginPath): void {
  replay.stop(); // a run's replay and one value's path do not mix
  origin.show(path, performance.now() / 1000);
  origin.place(originRoute);
  applyFilters(filters); // shades again, and says it in the status line
  fit(800, path.nodes.flatMap((id) => nodeById.get(id) ?? []));
}

/** The path is gone: the Origin view cleared it, or a live run or a replay started (then `tell` the view). */
function clearOrigin(tell: boolean): void {
  if (origin.path) {
    origin.clear();
    applyFilters(filters);
    if (tell) {
      vscode.postMessage({ type: "originCleared" });
    }
  }
}

/** The line a value moved along from one function to another (originlight.ts). */
function originRoute(from: string, to: string): Vec3[] | undefined {
  return routeBetween(from, to, links, nodeById);
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

/** Frames the whole brain (or `subset` of it). */
function fit(duration: number, subset: NeuronNode[] = nodes): void {
  if (subset.length) {
    wake(duration + 300);
    fitCamera(graph, subset, nodes.length <= ALWAYS_LABEL, duration);
  }
}

function flyTo(node: NeuronNode): void {
  wake(1100);
  flyToNode(graph, node);
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
document.getElementById("replayLatest")!.addEventListener("click", () => vscode.postMessage({ type: "replayLatest" }));
clearFiltersButton.addEventListener("click", () => vscode.postMessage({ type: "clearFilters" }));

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
    clearOrigin(true); // a run starting clears an origin path (as a replay does)
    setStatus(`starting ${message.script}…`);
  } else if (message.type === "live") {
    if (!live) {
      clearOrigin(true);
    }
    replay.stop(); // a program runs now: show it, not the past
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
  } else if (message.type === "replayStart") {
    clearOrigin(true);
    replay.start(message.label, message.start, message.end);
  } else if (message.type === "replayFrames") {
    replay.receive(message.seq, message.frames);
  } else if (message.type === "replayFailed") {
    replay.failed(message.seq, message.message);
  } else if (message.type === "replayStop") {
    replay.stop();
  } else if (message.type === "filters") {
    applyFilters(message.filters);
  } else if (message.type === "origin") {
    showOrigin(message.path);
  } else if (message.type === "originPulse") {
    origin.pulseNode(message.node, performance.now() / 1000);
  } else if (message.type === "originClear") {
    clearOrigin(false);
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
    replaying: () => replay.active,
  },
});

vscode.postMessage({ type: "ready" });
