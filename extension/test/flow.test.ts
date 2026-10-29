import assert from "node:assert/strict";
import { test } from "node:test";
import { addCall, alongPolyline, CROSSING, crossingTime, ease, FADE_OUT, HOLD, partOf, RISE, traceAt } from "../src/webview/flow";

test("the sweep takes its own short time to cross, a little longer on longer lines", () => {
  assert.equal(crossingTime(0), CROSSING.min);
  assert.equal(crossingTime(10_000), CROSSING.max);
  assert.ok(crossingTime(100) < crossingTime(300));
  assert.equal(ease(-1), 0);
  assert.equal(ease(2), 1);
});

test("a call draws the line from the caller to the callee, eased, then it fades back to grey", () => {
  const trace = addCall(undefined, 0, 0);
  const crossing = CROSSING.min;
  assert.deepEqual(traceAt(trace, 0), { head: 0, opacity: 1, fading: false });
  // Eased: slower at both ends than in the middle.
  assert.ok(traceAt(trace, 0.1 * crossing)!.head < 0.1);
  assert.ok(traceAt(trace, 0.5 * crossing)!.head > 0.45);
  assert.equal(traceAt(trace, crossing)!.head, 1);
  // Held a moment once drawn, then fading out.
  const holdEnd = Math.max(crossing, HOLD);
  assert.equal(traceAt(trace, holdEnd)!.opacity, 1);
  const fading = traceAt(trace, holdEnd + FADE_OUT / 2)!;
  assert.ok(fading.fading && fading.opacity > 0.4 && fading.opacity < 0.6);
  assert.equal(traceAt(trace, holdEnd + FADE_OUT), undefined);
});

test("a busy line stays drawn: calls neither stack nor restart the sweep", () => {
  let trace = addCall(undefined, 0, 100);
  for (let t = 0.05; t < 3; t += 0.05) {
    const again = addCall(trace, t, 100);
    assert.equal(again, trace);
    trace = again;
    const state = traceAt(trace, t)!;
    assert.equal(state.opacity, 1);
    assert.ok(t < trace.crossing || state.head === 1);
  }
  assert.equal(trace.start, 0);
  assert.ok(traceAt(trace, 3 + HOLD + FADE_OUT / 2)!.fading);
});

test("a call while the line fades brings it back up smoothly, without redrawing it", () => {
  const trace = addCall(undefined, 0, 0);
  const t = Math.max(CROSSING.min, HOLD) + FADE_OUT / 2;
  const before = traceAt(trace, t)!.opacity;
  addCall(trace, t, 0);
  const after = traceAt(trace, t)!;
  assert.ok(Math.abs(after.opacity - before) < 1e-9);
  assert.equal(after.head, 1);
  assert.ok(traceAt(trace, t + RISE / 2)!.opacity > before);
  assert.equal(traceAt(trace, t + RISE)!.opacity, 1);
});

test("an idle line starts a new sweep", () => {
  const trace = addCall(undefined, 0, 0);
  const next = addCall(trace, 10, 0);
  assert.notEqual(next, trace);
  assert.equal(traceAt(next, 10)!.head, 0);
});

test("the drawn part of a line ends exactly at the head", () => {
  const points = [
    { x: 0, y: 0, z: 0 },
    { x: 1, y: 0, z: 0 },
    { x: 4, y: 0, z: 0 },
  ];
  const { along } = alongPolyline(points);
  assert.deepEqual(partOf(points, along, 0), [points[0], { x: 0, y: 0, z: 0 }]);
  assert.deepEqual(partOf(points, along, 0.5), [points[0], points[1], { x: 2, y: 0, z: 0 }]);
  assert.equal(partOf(points, along, 1), points);
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
