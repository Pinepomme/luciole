/** Extension host: runs the Python analyzer, shows the graph in a webview panel, and runs live mode. */

import { ChildProcess, execFile } from "child_process";
import { randomBytes } from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import type { Graph } from "./graph";
import { webviewHtml } from "./html";
import { LiveSession } from "./live";
import type { FromWebview, PausedFrame, PausedThread, StepMode, ToWebview } from "./messages";
import { VariablesView } from "./variables";

let panel: GraphPanel | undefined;
let output: vscode.OutputChannel;
let live: LiveSession;
let variables: VariablesView;

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("Luciole");
  live = new LiveSession(
    {
      frame: (frame) => panel?.post({ type: "live", frame }),
      state: (state) => {
        if (!state.paused) {
          variables.clear();
        } else if (state.stepping) {
          variables.markStale(); // fresh snapshots come when the threads stop again
        }
        panel?.post({ type: "liveState", state });
      },
      paused: (thread) => {
        const first = variables.isEmpty;
        variables.add(thread);
        // After a step, the stepped thread stops first: show where it went.
        if (first && thread.frames[0]) {
          void vscode.commands.executeCommand("luciole.variables.focus");
          void revealFrame(thread.frames[0], thread);
        }
      },
    },
    output,
  );
  variables = new VariablesView((ref) => live.expand(ref));
  context.subscriptions.push(
    output,
    live,
    variables,
    vscode.commands.registerCommand("luciole.revealFrame", (frame: PausedFrame, thread: PausedThread) => revealFrame(frame, thread)),
    vscode.commands.registerCommand("luciole.stepOver", () => step("over")),
    vscode.commands.registerCommand("luciole.stepInto", () => step("into")),
    vscode.commands.registerCommand("luciole.stepOut", () => step("out")),
    vscode.commands.registerCommand("luciole.runLive", () => runLive(context, fileTarget())),
    vscode.commands.registerCommand("luciole.stopLive", () => live.stop()),
    vscode.commands.registerCommand("luciole.pauseLive", () => live.pause(true)),
    vscode.commands.registerCommand("luciole.resumeLive", () => live.pause(false)),
    vscode.commands.registerCommand("luciole.showGraph", () => showGraph(context, workspaceTarget(), false)),
    vscode.commands.registerCommand("luciole.showFileGraph", () => showGraph(context, fileTarget(), true)),
    // A file graph follows the editor: switching to another Python file shows that file's graph.
    vscode.window.onDidChangeActiveTextEditor((editor) => {
      const document = editor?.document;
      if (panel?.followsEditor && document?.languageId === "python" && document.uri.scheme === "file") {
        panel.show(document.uri.fsPath, true);
      }
    }),
    vscode.workspace.onDidSaveTextDocument((document) => {
      if (document.languageId === "python") {
        panel?.analyze();
      }
    }),
  );
}

export function deactivate(): void {
  panel?.dispose();
}

function workspaceTarget(): string | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders?.length) {
    void vscode.window.showErrorMessage("Luciole: open a folder first.");
    return undefined;
  }
  const active = vscode.window.activeTextEditor?.document.uri;
  const folder = (active && vscode.workspace.getWorkspaceFolder(active)) || folders[0];
  return folder.uri.fsPath;
}

function fileTarget(): string | undefined {
  const document = vscode.window.activeTextEditor?.document;
  if (!document || document.languageId !== "python" || document.uri.scheme !== "file") {
    void vscode.window.showErrorMessage("Luciole: open a Python file first.");
    return undefined;
  }
  return document.uri.fsPath;
}

/** Show the file's brain, then run the file under the live recorder in a terminal. */
async function runLive(context: vscode.ExtensionContext, script: string | undefined): Promise<void> {
  if (!script) {
    return;
  }
  const dir = analyzerDir(context);
  if (!dir) {
    void vscode.window.showErrorMessage("Luciole: the analyzer is missing from the extension.");
    return;
  }
  await vscode.workspace.textDocuments.find((d) => d.uri.fsPath === script && d.isDirty)?.save();
  if (!panel?.covers(script)) {
    showGraph(context, script, true); // a folder graph that includes the script stays, to light up the whole project
  }
  const root = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(script))?.uri.fsPath ?? path.dirname(script);
  panel?.post({ type: "liveStarted", script: path.basename(script) });
  await live.run(script, root, pythonPath(script), dir);
}

function step(mode: StepMode): void {
  const thread = variables.current;
  if (thread) {
    live.step(thread.id, mode);
  }
}

/** Show where a paused frame is: its line in the editor, its neuron in the brain. */
async function revealFrame(frame: PausedFrame, thread: PausedThread): Promise<void> {
  variables.focus(thread);
  const position = new vscode.Position(frame.line - 1, 0);
  await vscode.window.showTextDocument(vscode.Uri.file(frame.file), {
    viewColumn: vscode.ViewColumn.One,
    preserveFocus: true,
    selection: new vscode.Range(position, position),
  });
  panel?.post({ type: "focus", node: frame.node });
}

function showGraph(context: vscode.ExtensionContext, target: string | undefined, followEditor: boolean): void {
  if (!target) {
    return;
  }
  if (panel) {
    panel.show(target, followEditor);
  } else {
    panel = new GraphPanel(context, target, followEditor);
  }
}

class GraphPanel {
  private readonly panel: vscode.WebviewPanel;
  private ready = false;
  private running: ChildProcess | undefined;
  private root: string | undefined;

  constructor(
    private readonly context: vscode.ExtensionContext,
    private target: string,
    public followsEditor: boolean,
  ) {
    const extensionUri = context.extensionUri;
    this.panel = vscode.window.createWebviewPanel("luciole.graph", this.title(), vscode.ViewColumn.Beside, {
      enableScripts: true,
      retainContextWhenHidden: true,
      localResourceRoots: [vscode.Uri.joinPath(extensionUri, "out"), vscode.Uri.joinPath(extensionUri, "media")],
    });
    this.panel.iconPath = vscode.Uri.joinPath(extensionUri, "media", "icon.svg");
    const webview = this.panel.webview;
    webview.html = webviewHtml({
      cspSource: webview.cspSource,
      nonce: randomBytes(16).toString("hex"),
      scriptUri: webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, "out", "webview.js")).toString(),
      styleUri: webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, "media", "webview.css")).toString(),
    });
    webview.onDidReceiveMessage((message: FromWebview) => this.onMessage(message));
    this.panel.onDidDispose(() => {
      this.running?.kill();
      panel = undefined;
    });
  }

  show(target: string, followEditor: boolean): void {
    this.followsEditor = followEditor;
    if (target === this.target) {
      this.panel.reveal(undefined, followEditor); // keep the focus in the editor when following it
      return;
    }
    this.target = target;
    this.panel.title = this.title();
    this.panel.reveal(undefined, followEditor);
    this.analyze();
  }

  /** True when this panel shows a folder graph that includes `file`. */
  covers(file: string): boolean {
    const relative = path.relative(this.target, file);
    return !this.target.endsWith(".py") && relative !== "" && !relative.startsWith("..") && !path.isAbsolute(relative);
  }

  dispose(): void {
    this.panel.dispose();
  }

  private title(): string {
    return `Luciole: ${path.basename(this.target)}`;
  }

  post(message: ToWebview): void {
    void this.panel.webview.postMessage(message);
  }

  private onMessage(message: FromWebview): void {
    if (message.type === "ready") {
      this.ready = true;
      this.post({ type: "liveState", state: live.current });
      this.analyze();
    } else if (message.type === "refresh") {
      this.analyze();
    } else if (message.type === "runLive") {
      void runLive(this.context, this.target.endsWith(".py") ? this.target : fileTarget());
    } else if (message.type === "stopLive") {
      void live.stop();
    } else if (message.type === "pauseLive") {
      live.pause(message.paused);
    } else if (message.type === "step") {
      step(message.mode);
    } else if (message.type === "reveal" && this.root) {
      const position = new vscode.Position(message.line - 1, message.col);
      void vscode.window.showTextDocument(vscode.Uri.file(path.resolve(this.root, message.file)), {
        viewColumn: vscode.ViewColumn.One,
        preserveFocus: true,
        selection: new vscode.Range(position, position),
      });
    }
  }

  analyze(): void {
    if (!this.ready) {
      return; // the webview's "ready" message starts the first run
    }
    this.running?.kill();
    this.post({ type: "loading", target: path.basename(this.target) });
    this.running = runAnalyzer(this.context, this.target, (result) => {
      this.running = undefined;
      if ("error" in result) {
        this.post({ type: "error", message: result.error });
      } else {
        this.root = result.graph.root;
        this.post({ type: "graph", target: this.target, graph: result.graph, warnings: result.warnings });
      }
    });
  }
}

type AnalyzerResult = { graph: Graph; warnings: string[] } | { error: string };

/**
 * Runs `python -m luciole graph <target> --json`. The analyzer only needs the standard library,
 * so any Python 3.10+ works: its source is put on PYTHONPATH rather than installed.
 */
function runAnalyzer(context: vscode.ExtensionContext, target: string, done: (result: AnalyzerResult) => void): ChildProcess | undefined {
  const dir = analyzerDir(context);
  if (!dir) {
    done({ error: "The Luciole analyzer is missing from the extension." });
    return undefined;
  }
  const python = pythonPath(target);
  const args = ["-m", "luciole", "graph", path.resolve(target), "--json"];
  output.appendLine(`$ ${python} ${args.join(" ")}`);
  const started = Date.now();
  return execFile(
    python,
    args,
    { cwd: dir, env: { ...process.env, PYTHONPATH: dir }, maxBuffer: 1 << 30 },
    (error, stdout, stderr) => {
      const warnings = stderr.split("\n").filter((line) => line.startsWith("luciole: "));
      if (stderr) {
        output.append(stderr);
      }
      if (error) {
        if (error.killed || error.signal) {
          return; // replaced by a newer run
        }
        const reason = (error as NodeJS.ErrnoException).code === "ENOENT" ? `Python not found: ${python}` : stderr.trim().split("\n").slice(-3).join("\n") || error.message;
        done({ error: `The analyzer failed.\n${reason}\n\nLuciole needs Python 3.10 or newer. Choose one with the "luciole.pythonPath" setting.` });
        return;
      }
      output.appendLine(`analyzed in ${Date.now() - started} ms`);
      try {
        done({ graph: JSON.parse(stdout) as Graph, warnings });
      } catch (parseError) {
        done({ error: `The analyzer printed invalid JSON: ${parseError}` });
      }
    },
  );
}

/** Folder holding the `luciole` package: python/ in a packaged extension, ../src in this repo. */
function analyzerDir(context: vscode.ExtensionContext): string | undefined {
  return [path.join(context.extensionPath, "python"), path.join(context.extensionPath, "..", "src")].find((dir) =>
    fs.existsSync(path.join(dir, "luciole", "analyzer.py")),
  );
}

/** The configured interpreter, else the workspace's virtual environment, else python3. */
function pythonPath(target: string): string {
  const configured = vscode.workspace.getConfiguration("luciole").get<string>("pythonPath");
  if (configured) {
    return configured;
  }
  const folder = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(target))?.uri.fsPath;
  const windows = process.platform === "win32";
  for (const venv of [".venv", "venv"]) {
    const candidate = folder && path.join(folder, venv, windows ? "Scripts\\python.exe" : "bin/python");
    if (candidate && fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return windows ? "python" : "python3";
}
