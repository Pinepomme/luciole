/**
 * The replay bar under the brain: play/pause, a scrubber over the run, the time, the speed. It
 * drives a Timeline (timeline.ts), asks the extension for frames as it goes, and hands the frames
 * that come due to live mode's own lighting (`show`), so a replay looks like the run did live.
 */

import type { FromWebview, LiveFrame } from "../messages";
import { FRAME_MS, mergeFrames, SPEEDS, speedText, Timeline } from "./timeline";

export interface ReplayHooks {
  post(message: FromWebview): void;
  /** Light the brain with a frame, as live mode does. */
  show(frame: LiveFrame): void;
  /** The replay ended: the brain goes back to rest. */
  reset(): void;
  status(text: string): void;
}

/** Scrubber steps over the run. */
const STEPS = 1000;

export class ReplayBar {
  private readonly bar = document.getElementById("replay")!;
  private readonly playButton = document.getElementById("replayPlay") as HTMLButtonElement;
  private readonly scrubber = document.getElementById("replayScrub") as HTMLInputElement;
  private readonly time = document.getElementById("replayTime")!;
  private readonly speeds = document.getElementById("replaySpeed") as HTMLSelectElement;
  private timeline: Timeline | undefined;
  private label = "";
  private timer: ReturnType<typeof setInterval> | undefined;
  private last = 0;
  private dragging = false;

  constructor(private readonly hooks: ReplayHooks) {
    this.speeds.innerHTML = SPEEDS.map((speed) => `<option value="${speed}">${speedText(speed)}</option>`).join("");
    this.playButton.addEventListener("click", () => {
      const timeline = this.timeline;
      if (timeline) {
        if (timeline.playing) {
          timeline.playing = false;
        } else {
          timeline.play();
        }
        this.render();
      }
    });
    this.scrubber.addEventListener("input", () => {
      const timeline = this.timeline;
      if (timeline) {
        this.dragging = true;
        timeline.seek(timeline.start + ((timeline.end - timeline.start) * Number(this.scrubber.value)) / STEPS);
        this.render();
      }
    });
    this.scrubber.addEventListener("change", () => (this.dragging = false));
    this.speeds.addEventListener("change", () => {
      this.timeline?.setSpeed(Number(this.speeds.value));
      this.render();
    });
    document.getElementById("replayClose")!.addEventListener("click", () => {
      this.stop();
      this.hooks.post({ type: "replayClosed" });
    });
  }

  get active(): boolean {
    return this.timeline !== undefined;
  }

  /** Replays the run from `start` to `end` (ns), playing at once. */
  start(label: string, start: number, end: number): void {
    this.stop();
    this.label = label;
    this.timeline = new Timeline(start, Math.max(end, start + 1));
    this.speeds.value = String(this.timeline.speed);
    this.bar.hidden = false;
    this.timeline.play();
    this.last = performance.now();
    this.timer = setInterval(() => this.tick(), FRAME_MS);
    this.tick();
  }

  stop(): void {
    if (!this.timeline) {
      return;
    }
    clearInterval(this.timer);
    this.timer = undefined;
    this.timeline = undefined;
    this.bar.hidden = true;
    this.hooks.reset();
  }

  receive(seq: number, frames: LiveFrame[]): void {
    this.timeline?.receive(seq, frames);
  }

  failed(seq: number, message: string): void {
    this.timeline?.failed(seq);
    this.hooks.status(`replay: ${message}`);
  }

  private tick(): void {
    const timeline = this.timeline;
    if (!timeline) {
      return;
    }
    const now = performance.now();
    const due = timeline.tick(Math.min(now - this.last, 4 * FRAME_MS)); // a hidden panel's timers lag: no leap
    this.last = now;
    const request = timeline.request();
    if (request) {
      this.hooks.post({ type: "replayFetch", ...request });
    }
    const frame = mergeFrames(due);
    if (frame) {
      this.hooks.show(frame);
    }
    this.render();
  }

  private render(): void {
    const timeline = this.timeline;
    if (!timeline) {
      return;
    }
    const span = timeline.end - timeline.start;
    if (!this.dragging) {
      this.scrubber.value = String(Math.round(((timeline.position - timeline.start) / span) * STEPS));
    }
    this.playButton.textContent = timeline.playing ? "Pause" : "Play";
    this.time.textContent = `${seconds(timeline.position)} / ${seconds(timeline.end)}`;
    this.hooks.status(`replay of ${this.label} · ${speedText(timeline.speed)}${timeline.playing ? "" : " · paused"}`);
  }
}

/** Run time, to the precision its length needs. */
function seconds(ns: number): string {
  const s = ns / 1e9;
  return s >= 10 ? `${s.toFixed(1)} s` : s >= 0.01 ? `${s.toFixed(3)} s` : `${(s * 1000).toFixed(3)} ms`;
}
