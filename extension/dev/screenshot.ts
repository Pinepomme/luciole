/**
 * Renders the webview in headless Chromium, outside VS Code, and saves screenshots.
 * Uses the real webview bundle and Content Security Policy, with a stub of the VS Code API.
 *
 *   node out/dev/screenshot.js [path] [--events FILE [--frames N] [--every MS] [--zoom F]]
 *                              [--breakpoint FILE:LINE] [--stopped FILE:LINE] [--out dir] [--fps]
 *
 * --fps also prints how many frames per second the overview draws (and, with --frames, the replay).
 *
 * --events replays a recording made with `luciole run --events FILE ...` and captures the
 * moment with the deepest call stack. --frames N then keeps replaying it in real time (looping)
 * and captures N more shots, --every MS apart (default 200): 2-live-1.png ... 2-live-N.png, to see
 * how the light moves. --zoom F brings the camera F times closer for these shots.
 * --breakpoint and --stopped (FILE relative to the graph's
 * root) show a breakpoint and the debugger stopped there, as VS Code would send them.
 *
 * Needs a Chromium: set CHROME_PATH, or have one in the Playwright cache (~/.cache/ms-playwright).
 */

import { execFileSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { chromium } from "playwright-core";
import { webviewHtml } from "../src/html";

const extensionDir = path.resolve(__dirname, "../..");
const repo = path.resolve(extensionDir, "..");

// A few VS Code dark theme variables, so colors look as they do in the editor.
const THEME = `<style>:root {
  --vscode-editor-background: #1f1f1f; --vscode-editor-foreground: #cccccc; --vscode-foreground: #cccccc;
  --vscode-descriptionForeground: #9d9d9d; --vscode-editorWidget-background: #202020;
  --vscode-focusBorder: #0078d4; --vscode-panel-border: #2b2b2b; --vscode-errorForeground: #f85149;
  --vscode-button-secondaryBackground: #313131; --vscode-button-secondaryForeground: #cccccc;
  --vscode-button-secondaryHoverBackground: #3c3c3c;
  --vscode-font-family: system-ui, sans-serif; --vscode-font-size: 13px;
  --vscode-editor-font-family: "DejaVu Sans Mono", monospace;
  --vscode-charts-blue: #3794ff; --vscode-charts-green: #89d185; --vscode-charts-orange: #d18616;
  --vscode-charts-purple: #b180d7; --vscode-charts-yellow: #cca700; --vscode-charts-red: #f14c4c;
}</style>`;

function findChrome(): string {
  if (process.env.CHROME_PATH) {
    return process.env.CHROME_PATH;
  }
  const cache = path.join(os.homedir(), ".cache", "ms-playwright");
  const dirs = fs.existsSync(cache) ? fs.readdirSync(cache).filter((d) => d.startsWith("chromium-")).sort().reverse() : [];
  for (const dir of dirs) {
    for (const sub of ["chrome-linux64/chrome", "chrome-linux/chrome"]) {
      const candidate = path.join(cache, dir, sub);
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    }
  }
  throw new Error("No Chromium found: set CHROME_PATH.");
}

function option(name: string): string | undefined {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : undefined;
}

async function main(): Promise<void> {
  const target = path.resolve(process.argv[2] && !process.argv[2].startsWith("--") ? process.argv[2] : path.join(repo, "examples", "shop.py"));
  const outDir = path.resolve(option("--out") ?? path.join(extensionDir, "out", "dev", "shots"));
  fs.mkdirSync(outDir, { recursive: true });

  const json = execFileSync("python3", ["-m", "luciole", "graph", target, "--json"], {
    cwd: path.join(repo, "src"),
    env: { ...process.env, PYTHONPATH: path.join(repo, "src") },
    maxBuffer: 1 << 30,
  }).toString();

  const page_ = path.join(extensionDir, "out", "dev", "harness.html");
  const html = webviewHtml({ cspSource: "file:", nonce: "harness", scriptUri: "../webview.js", styleUri: "../../media/webview.css" });
  fs.writeFileSync(page_, html.replace("</head>", `${THEME}\n</head>`));

  // SwiftShader gives headless Chromium a software WebGL.
  const browser = await chromium.launch({ executablePath: findChrome(), args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 }, deviceScaleFactor: 1 });
  const problems: string[] = [];
  page.on("pageerror", (error) => problems.push(`page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" || message.type() === "warning") {
      problems.push(`console ${message.type()}: ${message.text()}`);
    }
  });
  await page.addInitScript(() => {
    const w = window as unknown as Record<string, unknown>;
    w.__posted = [];
    let state: unknown;
    w.acquireVsCodeApi = () => ({
      postMessage: (message: unknown) => (w.__posted as unknown[]).push(message),
      getState: () => state,
      setState: (s: unknown) => (state = s),
    });
  });

  // Poll with evaluate(): waitForFunction() evals a string, which the webview's CSP rightly blocks.
  const waitFor = async (predicate: () => boolean, what: string) => {
    for (let i = 0; i < 600; i++) {
      if (await page.evaluate(predicate)) {
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    throw new Error(`timed out waiting for ${what}`);
  };
  await page.goto(`file://${page_}`);
  await waitFor(() => ((window as any).__posted as { type: string }[]).some((m) => m.type === "ready"), "the webview to start");
  const ready = () => waitFor(() => document.body.dataset.state === "ready", "the layout");
  const shot = async (name: string) => {
    await ready();
    await page.waitForTimeout(800); // let the camera finish zooming to fit
    const file = path.join(outDir, `${name}.png`);
    await page.screenshot({ path: file });
    console.log(file);
  };

  await page.evaluate((graph) => window.postMessage({ type: "graph", target: "harness", graph, warnings: [] }, "*"), JSON.parse(json));
  await shot("1-overview");
  // Frames drawn in 3 s (software WebGL here: compare runs, not with a real GPU).
  const fps = async (what: string) => {
    if (process.argv.includes("--fps")) {
      const rate = await page.evaluate(
        () =>
          new Promise<number>((resolve) => {
            let frames = 0;
            const start = performance.now();
            const count = () => (performance.now() - start < 3000 ? (frames++, requestAnimationFrame(count)) : resolve(frames / 3));
            requestAnimationFrame(count);
          }),
      );
      console.log(`frames/s${what}: ${rate.toFixed(1)}`);
    }
  };
  await fps("");

  // Live lighting: replay recorded events (luciole run --events FILE), or a made-up frame for shop.py.
  const eventsFile = option("--events");
  const frames: { t?: number; stacks?: { stack: string[] }[] }[] = eventsFile
    ? fs.readFileSync(eventsFile, "utf8").split("\n").filter(Boolean).map((line) => JSON.parse(line)).filter((f) => !f.done)
    : target.endsWith("shop.py")
      ? [{ stacks: [{ thread: "MainThread", stack: ["shop", "shop:main", "shop:Cart.total", "shop:with_tax"] }], calls: { "shop:Cart.add": 2, "shop:with_tax": 1 }, links: { "shop:main>shop:Cart.add": 2 } }]
      : [];
  if (frames.length) {
    // Stop at the frame with the deepest stack, so the picture shows a pathway.
    // Most running plus waiting functions: the busiest moment.
    const depth = (f: { stacks?: { stack: string[] }[]; waiting?: Record<string, number> }) =>
      (f.stacks ?? []).reduce((sum, s) => sum + s.stack.length, 0) + Object.keys(f.waiting ?? {}).length;
    const deepest = frames.reduce((best, f, i) => (depth(f) > depth(frames[best]) ? i : best), 0);
    for (const frame of frames.slice(0, deepest + 1)) {
      await page.evaluate((frame) => window.postMessage({ type: "live", frame }, "*"), frame);
      await page.waitForTimeout(50);
    }
    await page.waitForTimeout(150);
    const zoom = Number(option("--zoom") ?? 1);
    if (zoom !== 1) {
      // Closer to the middle of the view, to see the light on a big graph.
      await page.evaluate((zoom) => {
        const graph = (window as any).luciole.graph;
        const target = graph.controls().target;
        const p = graph.camera().position;
        const at = (k: "x" | "y" | "z") => target[k] + (p[k] - target[k]) / zoom;
        graph.cameraPosition({ x: at("x"), y: at("y"), z: at("z") }, { x: target.x, y: target.y, z: target.z }, 0);
      }, zoom);
    }
    await shot("2-live");
    const count = Number(option("--frames") ?? 0);
    if (count > 0) {
      // Replay in the page, at the recording's pace and looping, while the shots are taken.
      await page.evaluate(
        ({ frames, from }) => {
          let i = from;
          const next = () => {
            const frame = frames[i];
            window.postMessage({ type: "live", frame }, "*");
            i = (i + 1) % frames.length;
            const gap = i > 0 && frames[i].t !== undefined && frame.t !== undefined ? (frames[i].t! - frame.t!) * 1000 : 50;
            setTimeout(next, Math.max(gap, 10));
          };
          next();
        },
        { frames, from: (deepest + 1) % frames.length },
      );
      await fps(" during the replay");
      const every = Number(option("--every") ?? 200);
      const start = Date.now();
      for (let i = 1; i <= count; i++) {
        await page.waitForTimeout(Math.max(0, start + i * every - Date.now()));
        const file = path.join(outDir, `2-live-${i}.png`);
        await page.screenshot({ path: file });
        console.log(`${file} (+${Date.now() - start} ms)`);
      }
    }
  }

  // The debugger: a breakpoint, and where it stopped.
  const sourceLine = (value: string | undefined) => {
    const [file, line] = value?.split(":") ?? [];
    return file && line ? { file, line: Number(line) } : undefined;
  };
  const breakpoint = sourceLine(option("--breakpoint"));
  const stopped = sourceLine(option("--stopped"));
  if (breakpoint || stopped) {
    await page.evaluate((breakpoints) => window.postMessage({ type: "breakpoints", breakpoints }, "*"), breakpoint ? [breakpoint] : []);
    await page.evaluate((at) => window.postMessage({ type: "debugStopped", at }, "*"), stopped);
    await page.waitForTimeout(1200); // the camera flies to the stop
    await shot("3-debugger");
  }

  const status = await page.textContent("#status");
  console.log(`status: ${status}`);
  await browser.close();
  if (problems.length) {
    console.error(problems.join("\n"));
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
