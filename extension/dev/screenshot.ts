/**
 * Renders the webview in headless Chromium, outside VS Code, and saves screenshots.
 * Uses the real webview bundle and Content Security Policy, with a stub of the VS Code API.
 *
 *   node out/dev/screenshot.js [path] [--events FILE [--frames N] [--every MS] [--zoom F]]
 *                              [--breakpoint FILE:LINE] [--stopped FILE:LINE] [--out dir] [--fps] [--cpu [--slow N] [--profile]]
 *                              [--theme light] [--focus ID] [--without-debugger]
 *
 * --focus shows what a click in the Map view does: the camera flies to the neuron ID
 * (`shop:Cart.add`) or the region ID (`mod:shop`, `cls:shop:Cart`, `pkg:src/app`).
 * --theme light uses VS Code's Light Modern colors instead of Dark Modern.
 * --cpu instead measures frame times with nothing drawn (what any machine pays, whatever its GPU):
 * idle, turning the camera, and the busiest neuron sliding back home after a drag. --slow N makes
 * the CPU N times slower (Chrome's throttling, as on a modest laptop); --profile also prints where
 * the time goes.
 * --fps also prints how many frames per second the overview draws (and, with --frames, the replay),
 * and its draw calls per frame (what a real GPU pays for: thousands of objects stutter).
 *
 * --events replays a recording made with `luciole run --events FILE ...` and captures the
 * moment with the deepest call stack. --frames N then keeps replaying it in real time (looping)
 * and captures N more shots, --every MS apart (default 200): 2-live-1.png ... 2-live-N.png, to see
 * how the light moves. --zoom F brings the camera F times closer for these shots.
 * --breakpoint and --stopped (FILE relative to the graph's
 * root) show a breakpoint and the debugger stopped there, as VS Code would send them.
 * --without-debugger shows a Run Live running in a terminal, the Python debugger not installed.
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

// A few VS Code theme variables (Dark Modern, or Light Modern), so colors look as they do in the editor.
const DARK = `
  --vscode-editor-background: #1f1f1f; --vscode-editor-foreground: #cccccc; --vscode-foreground: #cccccc;
  --vscode-descriptionForeground: #9d9d9d; --vscode-editorWidget-background: #202020;
  --vscode-focusBorder: #0078d4; --vscode-textLink-foreground: #4daafc; --vscode-panel-border: #2b2b2b;
  --vscode-errorForeground: #f85149; --vscode-button-background: #0078d4; --vscode-button-foreground: #ffffff;
  --vscode-button-secondaryBackground: #313131; --vscode-button-secondaryForeground: #cccccc;
  --vscode-button-secondaryHoverBackground: #3c3c3c; --vscode-debugIcon-breakpointForeground: #e51400;`;
const LIGHT = `
  --vscode-editor-background: #ffffff; --vscode-editor-foreground: #3b3b3b; --vscode-foreground: #3b3b3b;
  --vscode-descriptionForeground: #3b3b3b; --vscode-editorWidget-background: #f8f8f8;
  --vscode-focusBorder: #005fb8; --vscode-textLink-foreground: #005fb8; --vscode-panel-border: #e5e5e5;
  --vscode-errorForeground: #f85149; --vscode-button-background: #005fb8; --vscode-button-foreground: #ffffff;
  --vscode-button-secondaryBackground: #e5e5e5; --vscode-button-secondaryForeground: #3b3b3b;
  --vscode-button-secondaryHoverBackground: #cccccc; --vscode-debugIcon-breakpointForeground: #e51400;`;
const THEME = `<style>:root {${option("--theme") === "light" ? LIGHT : DARK}
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
    await page.screenshot({ path: file, timeout: 180_000 }); // big graphs draw slowly in software WebGL
    console.log(file);
  };

  if (process.argv.includes("--cpu")) {
    await cpuOnly(page, json);
    await browser.close();
    return;
  }
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
      const calls = await page.evaluate(() => (window as any).luciole.graph.renderer().info.render.calls as number);
      console.log(`frames/s${what}: ${rate.toFixed(1)} (${calls} draw calls)`);
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
        await page.screenshot({ path: file, timeout: 180_000 }); // big graphs draw slowly in software WebGL
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

  // The Map view: fly to a neuron, or to a region (an id starting with pkg:, mod: or cls:).
  const focus = option("--focus");
  if (focus) {
    const message = /^(pkg|mod|cls):/.test(focus) ? { type: "focusRegion", region: focus } : { type: "focus", node: focus };
    await page.evaluate((message) => window.postMessage(message, "*"), message);
    await page.waitForTimeout(1200); // the camera flies there
    await shot("4-focus");
  }

  if (process.argv.includes("--without-debugger")) {
    await page.evaluate(() => window.postMessage({ type: "debuggerNotice", show: true }, "*"));
    await shot("5-without-debugger");
  }

  const status = await page.textContent("#status");
  console.log(`status: ${status}`);
  await browser.close();
  if (problems.length) {
    console.error(problems.join("\n"));
    process.exit(1);
  }
}

/**
 * --cpu: frame times with nothing drawn (renderer.render does nothing), so what any machine pays
 * whatever its GPU: idle, then while a dragged neuron slides back home.
 */
async function cpuOnly(page: import("playwright-core").Page, json: string): Promise<void> {
  await page.evaluate(() => {
    // Nothing drawn, but objects still placed: render() is where three.js updates world matrices.
    (window as any).luciole.graph.renderer().render = (scene: any, camera: any) => {
      scene.updateMatrixWorld();
      if (camera.parent === null) {
        camera.updateMatrixWorld();
      }
    };
  });
  // --slow N: a CPU N times slower (Chrome's throttling), as on a modest laptop.
  const slow = Number(option("--slow") ?? 1);
  if (slow > 1) {
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Emulation.setCPUThrottlingRate", { rate: slow });
  }
  await page.evaluate((graph) => window.postMessage({ type: "graph", target: "harness", graph, warnings: [] }, "*"), JSON.parse(json));
  for (let i = 0; i < 600 && (await page.evaluate(() => document.body.dataset.state)) !== "ready"; i++) {
    await page.waitForTimeout(50);
  }
  await page.waitForTimeout(1000); // the camera's fit
  const frames = (ms: number) =>
    page.evaluate(
      (ms) =>
        new Promise<string>((resolve) => {
          const times: number[] = [];
          let last = performance.now();
          const start = last;
          const tick = (now: number) => {
            times.push(now - last);
            last = now;
            if (now - start < ms) {
              requestAnimationFrame(tick);
            } else {
              times.sort((a, b) => a - b);
              resolve(`${(times.length / (ms / 1000)).toFixed(0)} frames/s, median ${times[times.length >> 1].toFixed(1)} ms, worst ${times[times.length - 1].toFixed(0)} ms`);
            }
          };
          requestAnimationFrame(tick);
        }),
      ms,
    );
  // With --profile, where the time went: the functions that took the most, by their own time.
  const profiled = async (label: string, run: () => Promise<string>) => {
    if (!process.argv.includes("--profile")) {
      console.log(`${label}: ${await run()}`);
      return;
    }
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("Profiler.enable");
    await cdp.send("Profiler.start");
    const result = await run();
    const { profile } = await cdp.send("Profiler.stop");
    const byId = new Map(profile.nodes.map((node) => [node.id, node.callFrame]));
    const own = new Map<string, number>();
    (profile.samples ?? []).forEach((id, i) => {
      const frame = byId.get(id)!;
      const name = `${frame.functionName || "(anonymous)"} ${frame.url.split("/").pop()}:${frame.lineNumber + 1}`;
      own.set(name, (own.get(name) ?? 0) + (profile.timeDeltas?.[i] ?? 0) / 1000);
    });
    console.log(`${label}: ${result}`);
    [...own]
      .filter(([name]) => !name.startsWith("(idle)"))
      .sort((a, b) => b[1] - a[1])
      .slice(0, 14)
      .forEach(([name, ms]) => console.log(`   ${ms.toFixed(0).padStart(6)} ms  ${name}`));
  };
  await profiled("idle", () => frames(2000));
  // Turning: a drag across the background, then the camera easing to a stop.
  const box = await page.evaluate(() => {
    const rect = document.getElementById("brain")!.getBoundingClientRect();
    return { x: rect.left + 20, y: rect.top + 20 };
  });
  await page.mouse.move(box.x, box.y);
  await page.mouse.down();
  for (let i = 1; i <= 10; i++) {
    await page.mouse.move(box.x + 30 * i, box.y + 10 * i);
  }
  await page.mouse.up();
  await profiled("after turning the camera", () => frames(3000));
  await page.waitForTimeout(3000);
  await profiled("still again", () => frames(1500));
  // A neuron dragged away, then let go: it slides back home.
  {
    const at = await page.evaluate(() => {
      const graph = (window as any).luciole.graph;
      const node = graph.graphData().nodes.slice().sort((a: any, b: any) => b.neuron.degree - a.neuron.degree)[0];
      const screen = graph.graph2ScreenCoords(node.x, node.y, node.z);
      const rect = document.getElementById("brain")!.getBoundingClientRect();
      return { x: screen.x + rect.left, y: screen.y + rect.top, id: node.id };
    });
    await page.mouse.move(at.x - 3, at.y);
    await page.mouse.move(at.x, at.y);
    await page.waitForTimeout(400);
    const hovering = await page.evaluate(() => document.body.style.cursor === "pointer");
    await page.mouse.down();
    for (let i = 1; i <= 10; i++) {
      await page.mouse.move(at.x + 15 * i, at.y + 8 * i);
      await page.waitForTimeout(20);
    }
    const away = () =>
      page.evaluate((id) => {
        const node = (window as any).luciole.graph.graphData().nodes.find((n: any) => n.id === id);
        return Math.hypot(node.x - node.home.x, node.y - node.home.y, node.z - node.home.z).toFixed(0);
      }, at.id);
    console.log(`dragged ${at.id} ${await away()} units from its place${hovering ? "" : " (the pointer missed it)"}`);
    await page.mouse.up();
    await profiled("sliding back", () => frames(2500));
    const drawing = await page.evaluate(() => !(window as any).luciole.paused?.());
    console.log(`then: ${await away()} units from its place, ${drawing ? "still drawing" : "no longer drawing"}`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
