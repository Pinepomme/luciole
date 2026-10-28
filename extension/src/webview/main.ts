/**
 * Webview entry point: the 3D brain. Neurons are functions, synapses the variables flowing
 * between them. In live mode, functions light up as they run and pulses travel along synapses.
 */

import ForceGraph3D from "3d-force-graph";
import * as THREE from "three";
import SpriteText from "three-spritetext";
import type { Graph } from "../graph";
import type { FromWebview, LiveFrame, StepMode, ThreadStack, ToWebview } from "../messages";
import { Brain, buildBrain, Neuron, Synapse, SynapseKind, synapseId } from "./brain";

declare function acquireVsCodeApi(): { postMessage(message: FromWebview): void };

interface NeuronNode {
  id: string;
  neuron: Neuron;
  x?: number;
  y?: number;
  z?: number;
  /** 1 when the function just ran or is running, fading to 0. */
  heat: number;
  parts?: { material: THREE.MeshLambertMaterial; halo: THREE.Sprite; spark: THREE.Sprite; label?: SpriteText; radius: number; base: THREE.Color };
}

interface SynapseLink {
  id: string;
  source: string | NeuronNode;
  target: string | NeuronNode;
  synapse: Synapse;
  heat: number;
  line?: THREE.Line<THREE.BufferGeometry, THREE.LineBasicMaterial>;
}

/** Labels are always shown below this many neurons; above, only on lit or hovered ones. */
const ALWAYS_LABEL = 60;
/** Seconds for a flash to fade to about a third. */
const FADE = 0.7;
/** Glow kept by functions that ran during the current live session, once their flash fades. */
const VISITED = 0.12;
const MAX_PULSES_PER_LINK = 3;
/** Lines of the bottom bar: one per thread, then what is waiting. */
const MAX_PATH_LINES = 5;

const vscode = acquireVsCodeApi();
const statusEl = document.getElementById("status")!;
const pathEl = document.getElementById("path")!;
const messageEl = document.getElementById("message")!;
const liveButton = document.getElementById("live") as HTMLButtonElement;
const stopButton = document.getElementById("stop") as HTMLButtonElement;
const pauseButton = document.getElementById("pause") as HTMLButtonElement;
const steppingEl = document.getElementById("stepping")!;
const stepButtons = Array.from(steppingEl.querySelectorAll<HTMLButtonElement>("button[data-step]"));

const css = getComputedStyle(document.body);
const cssColor = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
const background = cssColor("--vscode-editor-background", "#1e1e1e");
const foreground = cssColor("--vscode-editor-foreground", "#cccccc");
/** Running. */
const AMBER = new THREE.Color("#ffc94d");
/** Suspended on an await. */
const BLUE = new THREE.Color("#5aa9ff");
const LINE_COLORS: Record<SynapseKind, THREE.Color> = {
  call: new THREE.Color("#6f8bb3"),
  state: new THREE.Color("#8a6fb3"),
  runtime: new THREE.Color("#d18616"),
  spawn: new THREE.Color("#4fb3a9"),
};
const glowTexture = makeGlowTexture();

let brain: Brain | undefined;
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
let paused = false;
let callsThisSecond: { t: number; n: number }[] = [];

const graph = new ForceGraph3D(document.getElementById("brain")!)
  .backgroundColor(background)
  .showNavInfo(false)
  .nodeId("id")
  .nodeThreeObject((node: object) => neuronObject(node as NeuronNode))
  .nodeLabel((node: object) => neuronTooltip((node as NeuronNode).neuron))
  .linkThreeObject((link: object) => synapseObject(link as SynapseLink))
  .linkPositionUpdate((object, { start, end }) => {
    const positions = (object as THREE.Line).geometry.getAttribute("position") as THREE.BufferAttribute;
    positions.setXYZ(0, start.x, start.y, start.z);
    positions.setXYZ(1, end.x, end.y, end.z);
    positions.needsUpdate = true;
    (object as THREE.Line).geometry.computeBoundingSphere();
    return true;
  })
  .linkLabel((link: object) => synapseTooltip((link as SynapseLink).synapse))
  .linkDirectionalParticleColor(() => "#ffc94d")
  .linkDirectionalParticleWidth(2.5)
  .linkDirectionalParticleSpeed(0.025)
  .cooldownTicks(300)
  .onNodeHover((node: object | null) => {
    hovered = (node as NeuronNode | null) ?? undefined;
    document.body.style.cursor = node ? "pointer" : "";
  })
  .onNodeClick((node: object) => reveal(node as NeuronNode));

let fitted = false;
graph.onEngineStop(() => {
  if (!fitted) {
    fitted = true;
    graph.zoomToFit(600, 40);
  }
  document.body.dataset.state = "ready";
});

// Three.js objects

function neuronObject(node: NeuronNode): THREE.Object3D {
  const radius = 2.5 + Math.sqrt(node.neuron.degree) * 1.5;
  const base = moduleColor(node.neuron.module);
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
  node.parts = { material, halo, spark, radius, base };
  return group;
}

function synapseObject(link: SynapseLink): THREE.Object3D {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(new Float32Array(6), 3));
  const material = new THREE.LineBasicMaterial({ color: LINE_COLORS[link.synapse.kind].clone(), transparent: true, opacity: baseOpacity(link) });
  link.line = new THREE.Line(geometry, material);
  return link.line;
}

function baseOpacity(link: SynapseLink): number {
  return { call: 0.55, state: 0.3, runtime: 0.2, spawn: 0.3 }[link.synapse.kind];
}

/** Each module gets its own soft hue, so modules read as regions of the brain. */
function moduleColor(module: string): THREE.Color {
  let hash = 0;
  for (const char of module) {
    hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  }
  return new THREE.Color().setHSL((hash % 360) / 360, 0.35, 0.55);
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

function label(node: NeuronNode): SpriteText {
  const parts = node.parts!;
  if (!parts.label) {
    parts.label = new SpriteText(node.neuron.label, 3.2, foreground);
    parts.label.fontFace = css.getPropertyValue("--vscode-editor-font-family").trim() || "monospace";
    parts.label.position.y = parts.radius + 4;
    parts.halo.parent!.add(parts.label);
  }
  return parts.label;
}

// Tooltips (text only: names come from the analyzed code)

function escape(text: string): string {
  return text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);
}

function neuronTooltip(neuron: Neuron): string {
  const name = neuron.node.kind === "module" ? `module ${neuron.label}` : `${neuron.label}(${neuron.params.join(", ")})`;
  return `<b>${escape(name)}</b><br><span class="dim">${escape(neuron.node.file)}:${neuron.node.loc.line}</span>`;
}

function synapseTooltip(synapse: Synapse): string {
  const title = { call: "calls", state: "shared state", runtime: "call seen at runtime", spawn: "starts a thread or task" }[synapse.kind];
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
    node.heat = onStack.has(node.id) ? 1 : Math.max(node.heat * fade, visited.has(node.id) ? VISITED : 0);
    const parts = node.parts;
    if (!parts) {
      continue;
    }
    // Waiting on an await shows in blue, unless the function is also running or just ran.
    const isWaiting = waiting.has(node.id) && node.heat < 0.5;
    const glow = isWaiting ? BLUE : AMBER;
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
    const wantsLabel = showAll || node.heat > 0.15 || isWaiting || node === hovered;
    if (wantsLabel || parts.label) {
      label(node).visible = wantsLabel;
    }
  }
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
  const previous = new Map(nodes.map((n) => [n.id, n]));
  nodes = brain.neurons.map((neuron) => {
    // Keep positions across refreshes of the same target, so the brain doesn't jump.
    const old = sameTarget ? previous.get(neuron.id) : undefined;
    return { id: neuron.id, neuron, heat: 0, x: old?.x, y: old?.y, z: old?.z };
  });
  nodeById.clear();
  nodes.forEach((n) => nodeById.set(n.id, n));
  links = brain.synapses.map((synapse) => ({ id: synapse.id, source: synapse.source, target: synapse.target, synapse, heat: 0 }));
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
        link = { id: synapse.id, source, target, synapse, heat: 1 };
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
stopButton.addEventListener("click", () => vscode.postMessage({ type: "stopLive" }));
pauseButton.addEventListener("click", () => vscode.postMessage({ type: "pauseLive", paused: !paused }));
for (const button of stepButtons) {
  button.addEventListener("click", () => vscode.postMessage({ type: "step", mode: button.dataset.step as StepMode }));
}
document.getElementById("refresh")!.addEventListener("click", () => vscode.postMessage({ type: "refresh" }));
document.getElementById("fit")!.addEventListener("click", () => graph.zoomToFit(600, 40));

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
  } else if (message.type === "focus") {
    const node = nodeById.get(message.node);
    if (node) {
      flyTo(node);
    }
  } else if (message.type === "liveState") {
    const { running, stopping, held, stepping } = message.state;
    paused = message.state.paused;
    stopButton.hidden = !running;
    stopButton.textContent = stopping ? "Force stop" : "Stop";
    pauseButton.hidden = !running || stopping;
    pauseButton.textContent = paused ? "Resume" : "Pause";
    pauseButton.classList.toggle("primary", paused);
    steppingEl.hidden = !running || stopping || !paused;
    stepButtons.forEach((button) => (button.disabled = !held || stepping));
    if (stopping) {
      setStatus("stopping…");
    } else if (stepping) {
      setStatus("stepping…");
    } else if (paused && !held) {
      setStatus("pausing… (threads stop at their next line of project code)");
    } else if (paused) {
      setStatus(`paused · ${held} thread${held > 1 ? "s" : ""} stopped · variables in the Luciole sidebar`);
    } else if (running && live) {
      setStatus("live · resumed");
    }
  }
});

// Hook for the browser harness in dev/ and for poking around from the webview devtools.
Object.assign(window, { luciole: { graph, live: onLive, isLive: () => live } });

vscode.postMessage({ type: "ready" });
