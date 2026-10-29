/**
 * The light that flows along synapses in live mode: a wave leaves the caller and glides to the
 * callee, brightest at its head with a fading tail. Waves keep their own slow pace, whatever the
 * speed of the calls: on a busy synapse they follow one another closely and merge into a steady
 * glow. Pure (no three.js): main.ts draws what this computes, in a shader that mirrors
 * `brightness`.
 */

/** One wave of light on a synapse. Times in seconds, positions along the line from 0 (caller) to 1 (callee). */
export interface Wave {
  start: number;
  /** Seconds for the head to cross the line and the tail to leave it. */
  duration: number;
  /** 0 to 1: more calls, a brighter wave. */
  strength: number;
}

/** Most waves at once on one synapse (the shader's array size). */
export const MAX_WAVES = 4;
/** Seconds between two waves leaving on one synapse: calls in between brighten the last one. */
export const WAVE_GAP = 0.3;
/** Seconds to cross, from short lines to long tracts. */
export const CROSSING = { min: 0.7, max: 1.2 } as const;
/** Length (world units) of a wave's tail on long lines; on short ones it is a share of the line. */
const TAIL = 45;
/** Synapses lit at once before their light dims, so a busy brain doesn't blow out where lines cross. */
const CROWD = 30;
/** How fast summed waves saturate: brightness is 1 - exp(-SATURATION * sum). */
const SATURATION = 2.0;

/** Seconds a wave takes on a line of this length (world units). */
export function crossingTime(length: number): number {
  return CROSSING.min + (CROSSING.max - CROSSING.min) * Math.min(1, Math.max(0, length / 400));
}

/** A wave's tail, as a share of a line of this length. */
export function tailLength(length: number): number {
  return Math.min(0.6, Math.max(0.15, TAIL / Math.max(length, 1)));
}

/**
 * `count` calls along a synapse at `now`: a new wave, unless one left less than WAVE_GAP ago or
 * the line is full, in which case the newest wave brightens. Returns the waves (changed in place).
 */
export function addCalls(waves: Wave[], count: number, now: number, length: number): Wave[] {
  const newest = waves[waves.length - 1];
  if (newest && (now - newest.start < WAVE_GAP || waves.length >= MAX_WAVES)) {
    newest.strength = Math.min(1, newest.strength + 0.15 * count);
  } else {
    waves.push({ start: now, duration: crossingTime(length), strength: 0.75 + 0.25 * (1 - 1 / (1 + count)) });
  }
  return waves;
}

/** Eased progress: slow to leave, slow to arrive. */
export function ease(p: number): number {
  return 0.5 - 0.5 * Math.cos(Math.PI * Math.min(1, Math.max(0, p)));
}

/** Where a wave's head is at `now`: from 0 to 1 + tail, so the tail leaves the line too. */
export function headAt(wave: Wave, now: number, tail: number): number {
  return ease((now - wave.start) / wave.duration) * (1 + tail);
}

/** Drops the waves that have left their line. */
export function prune(waves: Wave[], now: number): Wave[] {
  return waves.filter((wave) => now - wave.start < wave.duration);
}

/**
 * Light of one wave at `s` along the line, its head at `head`: 1 at the head, a short soft front
 * ahead of it, a tail fading behind.
 */
export function profile(s: number, head: number, tail: number): number {
  const behind = head - s;
  if (behind < 0) {
    const front = behind / (0.12 * tail);
    return Math.exp(-front * front);
  }
  const left = Math.max(0, 1 - behind / tail);
  return left * left;
}

/** The waves on a line as the shader takes them: head position and strength of each. */
export function waveState(waves: Wave[], now: number, tail: number): { head: number; strength: number }[] {
  return waves.map((wave) => {
    const p = (now - wave.start) / wave.duration;
    // Fades in as it leaves, so a wave doesn't pop up at full strength.
    return { head: headAt(wave, now, tail), strength: wave.strength * Math.min(1, p * 8) };
  });
}

/**
 * How much the whole line glows while waves pass along it, besides the waves themselves: up as a
 * wave leaves, down as it arrives.
 */
export function lineGlow(waves: Wave[], now: number): number {
  let glow = 0;
  for (const wave of waves) {
    const p = Math.min(1, Math.max(0, (now - wave.start) / wave.duration));
    glow = Math.max(glow, 0.12 * wave.strength * Math.sin(Math.PI * p));
  }
  return glow;
}

/**
 * Light at `s` along the line: the waves add up, plus the line's glow, and saturate towards 1
 * instead of blowing out. The fragment shader in main.ts computes the same.
 */
export function brightness(s: number, waves: { head: number; strength: number }[], tail: number, glow = 0): number {
  let sum = glow;
  for (const { head, strength } of waves) {
    sum += strength * profile(s, head, tail);
  }
  return 1 - Math.exp(-SATURATION * sum);
}

/**
 * How bright each line's light is when `flowing` synapses have some: full up to CROWD, then dimmer
 * as more light up, so that lines crossing or running together in tracts add up to a glow instead
 * of a white blur.
 */
export function crowdGain(flowing: number): number {
  return Math.min(1, Math.sqrt(CROWD / Math.max(flowing, 1)));
}

/** Positions along a polyline, 0 at its first point and 1 at its last, by length; and its length. */
export function alongPolyline(points: { x: number; y: number; z: number }[]): { along: number[]; length: number } {
  const along = [0];
  let length = 0;
  for (let i = 1; i < points.length; i++) {
    const [a, b] = [points[i - 1], points[i]];
    length += Math.hypot(b.x - a.x, b.y - a.y, b.z - a.z);
    along.push(length);
  }
  const last = Math.max(points.length - 1, 1);
  return { along: along.map((d, i) => (length > 0 ? d / length : i / last)), length };
}
