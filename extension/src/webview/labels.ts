/**
 * Names on the brain: a label per function (made the first time it shows), a label per region
 * (package, module, class), and declutterLabels, which keeps them one height on screen and hides
 * those that overlap a more important one.
 */

import * as THREE from "three";
import SpriteText from "three-spritetext";
import { labelAnchor, Layout, NEURON_GAP, Region, regionMembers } from "./layout";
import { NeuronNode, pickableWhenShown } from "./objects";
import { FONT, PRIMARY, SECONDARY } from "./theme";

/** Function names keep this height on screen, in pixels. */
const FUNCTION_LABEL_PX = 11;
/** Above this many shown labels, overlapping ones are not hidden (it costs pairs of labels). */
const MAX_DECLUTTERED = 400;
/** Region labels keep this height on screen, in pixels, like a map's place names. */
const REGION_LABEL_PX = { package: 15, module: 12, class: 11 } as const;
/** A region is named while it spans this many pixels on screen, and not once it fills the view (you are inside). */
const REGION_NAMED_MIN = 70;
const REGION_NAMED_MAX = 1.5;

/** Package, module and class names, above their regions. */
export const regionLabels = new THREE.Group();

/** A label per region (package, module, class), sized by the region; declutterLabels places them. */
export function makeRegionLabels(layout: Layout | undefined): void {
  for (const child of regionLabels.children) {
    (child as SpriteText).material.map?.dispose();
    (child as SpriteText).material.dispose();
  }
  regionLabels.clear();
  const members = layout ? regionMembers(layout) : new Map<string, string[]>();
  for (const region of layout?.byCalls ? [] : (layout?.regions.values() ?? [])) {
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

/** A function's name, made the first time it shows (most never do). */
export function label(node: NeuronNode): SpriteText {
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

const corner = new THREE.Vector3();
const cameraRight = new THREE.Vector3();
const cameraUp = new THREE.Vector3();

/**
 * Places region labels above their regions and keeps those of a readable size on screen, then
 * hides labels that overlap a more important one, so the ones left can be read. Region labels
 * come after functions that run, are hovered or hold a breakpoint, and before the others.
 * `priority` ranks a function's label (1000 per step of importance, see labelPriority in main.ts);
 * the region the Map points at, `focusedRegion`, is always named.
 */
export function declutterLabels(camera: THREE.Camera, nodes: NeuronNode[], nodeById: Map<string, NeuronNode>, focusedRegion: string | undefined, priority: (node: NeuronNode) => number): void {
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
      shown.push({ text, priority: priority(node) });
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
