import assert from "node:assert/strict";
import { test } from "node:test";
import { addCalls, alongPolyline, brightness, CROSSING, crossingTime, crowdGain, ease, headAt, lineGlow, MAX_WAVES, profile, prune, tailLength, Wave, WAVE_GAP, waveState } from "../src/webview/flow";

test("waves take their own time to cross, longer on longer lines, whatever the call rate", () => {
  assert.equal(crossingTime(0), CROSSING.min);
  assert.equal(crossingTime(10_000), CROSSING.max);
  assert.ok(crossingTime(100) < crossingTime(300));
  // A thousand calls at once still make one wave of the same pace.
  const waves = addCalls([], 1000, 5, 100);
  assert.equal(waves.length, 1);
  assert.equal(waves[0].duration, crossingTime(100));
  assert.ok(waves[0].strength <= 1);
});

test("calls close together brighten the last wave; spaced calls start new ones, up to a few", () => {
  const waves: Wave[] = [];
  addCalls(waves, 1, 0, 100);
  const first = waves[0].strength;
  addCalls(waves, 1, WAVE_GAP / 2, 100);
  assert.equal(waves.length, 1);
  assert.ok(waves[0].strength > first);
  for (let i = 1; i <= 10; i++) {
    addCalls(waves, 1, i * WAVE_GAP, 100);
  }
  assert.equal(waves.length, MAX_WAVES);
  assert.ok(waves.every((w) => w.strength <= 1));
});

test("a wave leaves the caller, reaches the callee eased, then its tail leaves too", () => {
  const wave: Wave = { start: 0, duration: 1, strength: 1 };
  const tail = 0.3;
  assert.equal(headAt(wave, 0, tail), 0);
  assert.ok(Math.abs(headAt(wave, 1, tail) - (1 + tail)) < 1e-9);
  // Eased: slower at both ends than in the middle.
  assert.ok(headAt(wave, 0.1, tail) < 0.1 * (1 + tail));
  assert.ok(headAt(wave, 0.5, tail) > 0.45 * (1 + tail));
  assert.equal(ease(-1), 0);
  assert.equal(ease(2), 1);
  assert.deepEqual(prune([wave], 0.5), [wave]);
  assert.deepEqual(prune([wave], 1), []);
});

test("brightest at the head, a fading tail behind, a short front ahead", () => {
  const tail = 0.3;
  assert.equal(profile(0.5, 0.5, tail), 1);
  assert.ok(profile(0.4, 0.5, tail) > profile(0.3, 0.5, tail));
  assert.equal(profile(0.5 - tail, 0.5, tail), 0);
  assert.ok(profile(0.52, 0.5, tail) < profile(0.48, 0.5, tail));
  assert.ok(profile(0.6, 0.5, tail) < 0.01);
});

test("waves add up and saturate instead of blowing out", () => {
  const one = brightness(0.5, [{ head: 0.5, strength: 1 }], 0.3);
  const many = brightness(0.5, Array.from({ length: MAX_WAVES }, () => ({ head: 0.5, strength: 1 })), 0.3);
  assert.ok(one > 0.5 && one < 1);
  assert.ok(many > one && many < 1);
  assert.equal(brightness(0.5, [], 0.3), 0);
  // The line's own glow lights it all along.
  assert.ok(brightness(0.1, [], 0.3, 0.2) > 0);
});

test("a new wave fades in, and the line glows while waves pass", () => {
  const waves: Wave[] = [{ start: 0, duration: 1, strength: 1 }];
  assert.equal(waveState(waves, 0, 0.3)[0].strength, 0);
  assert.equal(waveState(waves, 0.5, 0.3)[0].strength, 1);
  assert.equal(lineGlow(waves, 0), 0);
  assert.ok(lineGlow(waves, 0.5) > lineGlow(waves, 0.1));
  assert.equal(lineGlow([], 0.5), 0);
});

test("many lit synapses dim together, a few keep their full light", () => {
  assert.equal(crowdGain(0), 1);
  assert.equal(crowdGain(10), 1);
  assert.ok(crowdGain(1000) < crowdGain(100));
  assert.ok(crowdGain(1000) > 0);
});

test("tails are a share of short lines and a fixed length on long tracts", () => {
  assert.equal(tailLength(10), 0.6);
  assert.equal(tailLength(10_000), 0.15);
  assert.ok(tailLength(100) > tailLength(200));
});

test("positions along a polyline go by length", () => {
  const { along, length } = alongPolyline([
    { x: 0, y: 0, z: 0 },
    { x: 1, y: 0, z: 0 },
    { x: 4, y: 0, z: 0 },
  ]);
  assert.equal(length, 4);
  assert.deepEqual(along, [0, 0.25, 1]);
  // Both ends in one place (a neuron calling itself): spread evenly.
  assert.deepEqual(alongPolyline([{ x: 1, y: 1, z: 1 }, { x: 1, y: 1, z: 1 }]).along, [0, 1]);
});
