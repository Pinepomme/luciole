/**
 * How a synapse shows a call in live mode: the line traces itself from the caller to the callee
 * in the accent color, like a route drawing itself on a map, then eases back to grey. A busy line
 * stays drawn until its calls stop: calls never stack up or restart the sweep. Pure (no
 * three.js): objects.ts draws the part of the line this says is drawn.
 */

/** A call traced along a synapse. Times in seconds. */
export interface Trace {
  /** When the sweep left the caller. */
  start: number;
  /** Seconds for the sweep to reach the callee. */
  crossing: number;
  /** The latest call: the line stays drawn until HOLD after it. */
  last: number;
  /** A call during the fade: the line comes back up from `from`, its opacity then. */
  rise?: { at: number; from: number };
}

/** Seconds for the sweep to cross, from short lines to long tracts. */
export const CROSSING = { min: 0.3, max: 0.4 } as const;
/** Seconds a line stays drawn after its last call (live frames come every 50 ms). */
export const HOLD = 0.25;
/** Seconds to ease back to grey. */
export const FADE_OUT = 0.5;
/** Seconds for a fading line to come back up when called again. */
export const RISE = 0.15;

/** Seconds the sweep takes on a line of this length (world units). */
export function crossingTime(length: number): number {
  return CROSSING.min + (CROSSING.max - CROSSING.min) * Math.min(1, Math.max(0, length / 400));
}

/** Eased progress: slow to leave, slow to arrive. */
export function ease(p: number): number {
  return 0.5 - 0.5 * Math.cos(Math.PI * Math.min(1, Math.max(0, p)));
}

/**
 * How much of the line is drawn at `now` (`head`, 0 at the caller to 1 at the callee) and how
 * strongly (`opacity`, 0 to 1); undefined once it is back to grey.
 */
export function traceAt(trace: Trace, now: number): { head: number; opacity: number; fading: boolean } | undefined {
  const head = ease((now - trace.start) / trace.crossing);
  const rise = trace.rise ? trace.rise.from + (1 - trace.rise.from) * ease((now - trace.rise.at) / RISE) : 1;
  const holdEnd = Math.max(trace.start + trace.crossing, trace.last + HOLD);
  if (now <= holdEnd) {
    return { head, opacity: rise, fading: false };
  }
  const fade = (now - holdEnd) / FADE_OUT;
  return fade >= 1 ? undefined : { head, opacity: rise * (1 - ease(fade)), fading: true };
}

/**
 * A call along a synapse at `now`: a new sweep on an idle line; on a line being drawn or held,
 * it only keeps the line drawn longer; on a fading one, the line comes back up where it is.
 */
export function addCall(trace: Trace | undefined, now: number, length: number): Trace {
  const state = trace && traceAt(trace, now);
  if (!trace || !state) {
    return { start: now, crossing: crossingTime(length), last: now };
  }
  if (state.fading) {
    trace.rise = { at: now, from: state.opacity };
  }
  trace.last = Math.max(trace.last, now);
  return trace;
}

/**
 * The drawn part of a polyline whose points are at `along` (0 to 1): the points before `head`,
 * then the point at `head` itself, so the stroke ends exactly there.
 */
export function partOf<P extends { x: number; y: number; z: number }>(points: P[], along: number[], head: number): { x: number; y: number; z: number }[] {
  if (head >= 1) {
    return points;
  }
  const out: { x: number; y: number; z: number }[] = [points[0]];
  for (let i = 1; i < points.length; i++) {
    if (along[i] < head) {
      out.push(points[i]);
      continue;
    }
    const span = along[i] - along[i - 1];
    const t = span > 0 ? (head - along[i - 1]) / span : 1;
    const [a, b] = [points[i - 1], points[i]];
    out.push({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t, z: a.z + (b.z - a.z) * t });
    break;
  }
  return out;
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
