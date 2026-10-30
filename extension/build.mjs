// Bundles the extension host, the webview, the tests and the dev harness with esbuild.
import * as esbuild from "esbuild";

const common = { bundle: true, sourcemap: true, logLevel: "info" };
const node = { ...common, platform: "node", format: "cjs", target: "node18" };
const builds = [
  { ...node, entryPoints: ["src/extension.ts"], outfile: "out/extension.js", external: ["vscode"] },
  { ...common, entryPoints: ["src/webview/main.ts"], outfile: "out/webview.js", platform: "browser", format: "iife", target: "es2020" },
  { ...node, entryPoints: ["test/brain.test.ts"], outfile: "out/test/brain.test.js" },
  { ...node, entryPoints: ["test/layout.test.ts"], outfile: "out/test/layout.test.js" },
  { ...node, entryPoints: ["test/flow.test.ts"], outfile: "out/test/flow.test.js" },
  { ...node, entryPoints: ["test/runs.test.ts"], outfile: "out/test/runs.test.js" },
  { ...node, entryPoints: ["test/processes.test.ts"], outfile: "out/test/processes.test.js" },
  { ...node, entryPoints: ["test/engine.test.ts"], outfile: "out/test/engine.test.js" },
  { ...node, entryPoints: ["test/sidebar.test.ts"], outfile: "out/test/sidebar.test.js" },
  { ...node, entryPoints: ["test/history.test.ts"], outfile: "out/test/history.test.js" },
  { ...node, entryPoints: ["dev/screenshot.ts"], outfile: "out/dev/screenshot.js", external: ["playwright-core"] },
  { ...node, entryPoints: ["dev/layout-stats.ts"], outfile: "out/dev/layout-stats.js" },
];

if (process.argv.includes("--watch")) {
  for (const options of builds) {
    await (await esbuild.context(options)).watch();
  }
} else {
  await Promise.all(builds.map((options) => esbuild.build(options)));
}
