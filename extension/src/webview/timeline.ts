/**
 * Replaying a recorded run on the brain: where the replay is in the run, how fast it goes, and
 * which frames to ask for next. The frames are live frames made from the trace by Python
 * (`luciole trace FILE frames`, one per FRAME_MS of replay), asked for a chunk at a time just
 * ahead of the position, so a run of any size is never loaded whole; a jump drops what was
 * buffered and asks for the frame at the new position. Pure logic (no DOM): replay.ts draws it.
 */

import type { LiveFrame } from "../messages";

/** Replay time per frame (ms): about what live mode gets from a running program. */
export const FRAME_MS = 50;
/** Frames asked for at once. */
export const CHUNK = 20;
/** Replay speeds, as a share of the recording's own pace. */
export const SPEEDS = [0.0001, 0.001, 0.01, 0.1, 1, 10];

export interface FrameRequest {
  seq: number;
  /** Nanoseconds since the run started. */
  from: number;
  to: number;
  every: number;
}

/** The speed for a run of `durationNs`: the fastest that still takes 8 s or more to play it. */
export function defaultSpeed(durationNs: number): number {
  const fitting = SPEEDS.filter((speed) => durationNs / speed >= 8e9);
  return fitting.length ? fitting[fitting.length - 1] : SPEEDS[0];
}

export function speedText(speed: number): string {
  return speed < 1 ? `1/${Math.round(1 / speed)}×` : `${speed}×`;
}

/** Several frames due at once, as one: the calls added up, the last stacks. */
export function mergeFrames(frames: LiveFrame[]): LiveFrame | undefined {
  if (frames.length <= 1) {
    return frames[0];
  }
  const sum = (key: "calls" | "links" | "spawns") => {
    const total: Record<string, number> = {};
    for (const frame of frames) {
      for (const [name, count] of Object.entries(frame[key] ?? {})) {
        total[name] = (total[name] ?? 0) + count;
      }
    }
    return total;
  };
  const last = frames[frames.length - 1];
  return { t: last.t, stacks: last.stacks, calls: sum("calls"), links: sum("links"), spawns: sum("spawns") };
}

export class Timeline {
  /** Where the replay is (ns since the run started). */
  position: number;
  playing = false;
  speed: number;
  /** Frames received and not shown yet, in order; each covers the `period` before its `t`. */
  private frames: LiveFrame[] = [];
  /** Frames were asked for up to here. */
  private asked: number;
  private pending: FrameRequest | undefined;
  private seq = 0;

  constructor(
    readonly start: number,
    readonly end: number,
  ) {
    this.position = start;
    this.asked = start;
    this.speed = defaultSpeed(end - start);
  }

  /** Recording time per frame (ns). */
  get period(): number {
    return Math.max(1, Math.round(FRAME_MS * 1e6 * this.speed));
  }

  /** The next frames to ask for, if none are on their way and fewer than half a chunk is buffered. */
  request(): FrameRequest | undefined {
    if (this.pending || this.asked >= this.end || this.asked - this.position >= (CHUNK / 2) * this.period) {
      return undefined;
    }
    const from = this.asked;
    const to = Math.min(this.end, from + CHUNK * this.period);
    this.pending = { seq: this.seq, from, to, every: this.period };
    return this.pending;
  }

  /** Frames for request `seq` came; those of a request made before a jump are dropped. */
  receive(seq: number, frames: LiveFrame[]): void {
    if (!this.pending || seq !== this.pending.seq) {
      return;
    }
    this.frames.push(...frames);
    this.asked = this.pending.to;
    this.pending = undefined;
  }

  /** The request in flight failed: ask again next time. */
  failed(seq: number): void {
    if (this.pending?.seq === seq) {
      this.pending = undefined;
    }
  }

  /**
   * `ms` of replay went by: the position moves on (while playing, never past what was received,
   * so a slow answer stalls the replay instead of skipping frames), and the frames now due come
   * out, in order.
   */
  tick(ms: number): LiveFrame[] {
    if (this.playing) {
      const limit = this.asked >= this.end && !this.pending ? this.end : Math.min(this.end, this.asked);
      this.position = Math.min(limit, this.position + ms * 1e6 * this.speed);
      if (this.position >= this.end) {
        this.playing = false;
      }
    }
    const due: LiveFrame[] = [];
    while (this.frames.length && Math.round((this.frames[0].t ?? 0) * 1e9) <= this.position + 1) {
      due.push(this.frames.shift()!);
    }
    return due;
  }

  /** Jump to `ns`: the next frame asked for ends there, so the brain shows that moment at once. */
  seek(ns: number): void {
    this.position = Math.max(this.start, Math.min(this.end, ns));
    this.restart(Math.max(0, this.position - this.period));
  }

  setSpeed(speed: number): void {
    this.speed = speed;
    this.restart(this.position);
  }

  /** At the end: play again from the start. */
  play(): void {
    if (this.position >= this.end) {
      this.seek(this.start);
    }
    this.playing = true;
  }

  private restart(from: number): void {
    this.frames = [];
    this.pending = undefined;
    this.asked = from;
    this.seq++;
  }
}
