/**
 * The brain's colors and font, read from VS Code's theme (its CSS variables): neutral greys at
 * rest, a muted tint per file, one accent for activity, red only for the debugger. Also the
 * textures of rings and breakpoint dots.
 */

import * as THREE from "three";

const css = getComputedStyle(document.body);
const cssColor = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
export const background = cssColor("--vscode-editor-background", "#1e1e1e");
const foreground = cssColor("--vscode-editor-foreground", "#cccccc");
const BACKGROUND = new THREE.Color(background);
const FOREGROUND = new THREE.Color(foreground);
/** A grey between the background and the text: 0 is the background, 1 the text. */
const grey = (share: number) => BACKGROUND.clone().lerp(FOREGROUND, share);
/** Light themes: thin grey strokes and text fade into white, so they need a darker grey. */
const LIGHT_THEME = BACKGROUND.getHSL({ h: 0, s: 0, l: 0 }).l > 0.5;
/** Text that names things, quieter than the code. */
export const SECONDARY = `#${grey(LIGHT_THEME ? 0.75 : 0.62).getHexString()}`;
export const PRIMARY = `#${grey(0.92).getHexString()}`;
/** Neurons at rest, and lines. */
const NEUTRAL = grey(LIGHT_THEME ? 0.45 : 0.4);
export const LINE = grey(LIGHT_THEME ? 0.8 : 0.55);
/**
 * Each file's neurons get a muted tint, so you see at a glance which module a function is in:
 * Apple's system hues (no red: breakpoints and stops), half-way to the neutral grey so they stay
 * quiet next to the accent. Picked by a hash of the module, so a module keeps its tint.
 */
const MODULE_HUES = ["#30B0C7", "#34C759", "#5856D6", "#AF52DE", "#FF9F0A", "#00C7BE", "#7D7AFF", "#A2845E", "#64D2FF", "#FFD60A"];
const MODULE_TINT = LIGHT_THEME ? 0.55 : 0.5;
export function moduleTint(module: string): THREE.Color {
  let hash = 2166136261;
  for (let i = 0; i < module.length; i++) {
    hash = Math.imul(hash ^ module.charCodeAt(i), 16777619);
  }
  return NEUTRAL.clone().lerp(new THREE.Color(MODULE_HUES[(hash >>> 0) % MODULE_HUES.length]), MODULE_TINT);
}
/** Activity (running, a call, the call path, waiting): the theme's accent. */
export const ACCENT = new THREE.Color(themeAccent());
/** Where the debugger stopped, and breakpoints (VS Code's breakpoint red). */
export const RED = new THREE.Color(cssColor("--vscode-debugIcon-breakpointForeground", "#e51400"));
/** The ring around what the Map view points at: the text color, not the accent (it is not activity). */
export const FOCUS = new THREE.Color(PRIMARY);
export const ringTexture = makeRingTexture();
export const dotTexture = makeDotTexture();

/** The system's interface font, as on macOS, then VS Code's. */
export const FONT = `-apple-system, BlinkMacSystemFont, "SF Pro Text", ${css.getPropertyValue("--vscode-font-family").trim() || "system-ui"}, sans-serif`;

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
