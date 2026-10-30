/**
 * Prints how the region layout of a graph turns out, and how long it takes:
 *
 *   node out/dev/layout-stats.js GRAPH.json
 *
 * For each top-level region: neurons, radius, and how full it is (neuron volume / sphere volume).
 */

import * as fs from "fs";
import type { Graph } from "../src/graph";
import { buildBrain } from "../src/webview/brain";
import { layoutBrain, length, neuronRadius } from "../src/webview/layout";

const graph: Graph = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const brain = buildBrain(graph);
const started = performance.now();
const layout = layoutBrain(brain, new Map(graph.nodes.map((n) => [n.id, n])));
const elapsed = performance.now() - started;
const root = layout.regions.get("")!;
console.log(`${brain.neurons.length} neurons, ${brain.synapses.length} synapses, ${layout.regions.size} regions, radius ${root.radius.toFixed(0)}, layout ${elapsed.toFixed(0)} ms`);
const degree = new Map(brain.neurons.map((n) => [n.id, n.degree]));
const top = root.regions.map((id) => layout.regions.get(id)!).sort((a, b) => b.size - a.size);
for (const region of top.slice(0, 15)) {
  const inside = [...layout.chains].filter(([, chain]) => chain.includes(region.id)).map(([id]) => id);
  const volume = inside.reduce((v, id) => v + neuronRadius(degree.get(id)!) ** 3, 0);
  const spread = inside.reduce((d, id) => {
    const p = layout.positions.get(id)!;
    return d + length({ x: p.x - region.center.x, y: p.y - region.center.y, z: p.z - region.center.z });
  }, 0) / inside.length;
  console.log(
    `${region.label.padEnd(24)} ${region.kind.padEnd(8)} ${String(region.size).padStart(5)} r=${region.radius.toFixed(0).padStart(5)} mean=${spread.toFixed(0).padStart(5)} fill=${(volume / region.radius ** 3).toFixed(3)} children=${region.regions.length}`,
  );
}

// With a region's label as second argument: where its child regions are.
const name = process.argv[3];
const region = name ? [...layout.regions.values()].find((r) => r.label === name) : undefined;
if (region) {
  console.log(`\n${region.label}: r=${region.radius.toFixed(0)}`);
  for (const id of region.regions) {
    const child = layout.regions.get(id)!;
    const c = child.center;
    const distance = length({ x: c.x - region.center.x, y: c.y - region.center.y, z: c.z - region.center.z });
    console.log(`  ${child.label.padEnd(30)} size=${child.size} r=${child.radius.toFixed(0)} distance=${distance.toFixed(0)}`);
  }
}
