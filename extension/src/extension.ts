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
import { RunConfiguration, fileRunKind, pytestRun, resolveRun, splitArgs } from "./runs";
import { VariablesView } from "./variables";

let panel: GraphPanel | undefined;
let output: vscode.OutputChannel;
let live: LiveSession;
let variables: VariablesView;
let autoStartItem: vscode.StatusBarItem;

/** The last run started, to run it again from the panel. */
const LAST_RUN = "luciole.lastRun";

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
      started: (label) => {
        // A program started in a terminal with auto-start on: show the project it lights up.
        if (!panel && vscode.workspace.workspaceFolders?.length) {
          showGraph(context, workspaceTarget(), false);
        }
        if (!live.current.running) {
          panel?.post({ type: "liveStarted", script: label });
        }
      },
    },
    output,
    {
      python: () => pythonPath(vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd()),
      analyzerDir: () => analyzerDir(context),
      version: String(context.extension.packageJSON.version),
    },
  );
  autoStartItem = vscode.window.createStatusBarItem("luciole.autoStart", vscode.StatusBarAlignment.Left);
  autoStartItem.name = "Luciole Auto-Start";
  autoStartItem.command = "luciole.toggleAutoStart";
  variables = new VariablesView((ref) => live.expand(ref));
  context.subscriptions.push(
    output,
    live,
    variables,
    vscode.commands.registerCommand("luciole.revealFrame", (frame: PausedFrame, thread: PausedThread) => revealFrame(frame, thread)),
    vscode.commands.registerCommand("luciole.stepOver", () => step("over")),
    vscode.commands.registerCommand("luciole.stepInto", () => step("into")),
    vscode.commands.registerCommand("luciole.stepOut", () => step("out")),
    vscode.commands.registerCommand("luciole.runLive", () => runFile(context, fileTarget())),
    vscode.commands.registerCommand("luciole.runLiveWith", () => pickRun(context)),
    vscode.commands.registerCommand("luciole.stopLive", () => live.stop()),
    vscode.commands.registerCommand("luciole.pauseLive", () => live.pause(true)),
    vscode.commands.registerCommand("luciole.resumeLive", () => live.pause(false)),
    vscode.commands.registerCommand("luciole.showGraph", () => showGraph(context, workspaceTarget(), false)),
    vscode.commands.registerCommand("luciole.showFileGraph", () => showGraph(context, fileTarget(), true)),
    autoStartItem,
    vscode.commands.registerCommand("luciole.toggleAutoStart", () => {
      const settings = vscode.workspace.getConfiguration("luciole");
      return settings.update("autoStart", !settings.get<boolean>("autoStart"), vscode.ConfigurationTarget.Global);
    }),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("luciole.autoStart")) {
        void applyAutoStart(context);
      }
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => applyAutoStart(context)),
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
  void applyAutoStart(context);
}

/**
 * Auto-start: Python programs started in VS Code's terminals light up the view, with no change to
 * how they are run. The switch only sets environment variables for new terminals: the auto-start
 * folder on PYTHONPATH, whose sitecustomize starts the recorder when the engine runs (found through
 * its file), and the project root. Off, nothing is set. Terminals already open keep what they had
 * (VS Code flags them as stale).
 */
async function applyAutoStart(context: vscode.ExtensionContext): Promise<void> {
  const on = vscode.workspace.getConfiguration("luciole").get<boolean>("autoStart", false);
  const env = context.environmentVariableCollection;
  env.persistent = false; // the extension's folder changes with its version
  env.clear();
  const folders = vscode.workspace.workspaceFolders ?? [];
  for (const folder of folders) {
    env.getScoped({ workspaceFolder: folder }).clear();
  }
  const dir = analyzerDir(context);
  if (on && dir) {
    env.description = "Luciole auto-start: Python programs started here light up the Luciole view.";
    // Prepended, so that PYTHONPATH set elsewhere (Run Live, the user's) stays; when PYTHONPATH is
    // empty, the auto-start folder drops the empty entry this leaves.
    env.prepend("PYTHONPATH", path.join(dir, "luciole", "_autostart") + path.delimiter);
    void live.connect().catch((error) => output.appendLine(`Luciole engine: ${error}`)); // programs find it as they start
    for (const folder of folders) {
      env.getScoped({ workspaceFolder: folder }).replace("LUCIOLE_ROOT", folder.uri.fsPath);
    }
  }
  autoStartItem.text = on ? "$(zap) Luciole: on" : "$(circle-slash) Luciole: off";
  autoStartItem.tooltip = on
    ? "Python programs started in new terminals light up the Luciole view. Click to turn off."
    : "Click to light up the Luciole view with every Python program started in new terminals (pytest, scripts, servers).";
  autoStartItem.show();
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

/**
 * Run a Python file the way it is meant to run (see fileRunKind): a test file with pytest, a
 * script as itself, a library module through the tests that import it, else as a script.
 */
async function runFile(context: vscode.ExtensionContext, file: string | undefined): Promise<void> {
  if (!file) {
    return;
  }
  const name = path.basename(file);
  const open = vscode.workspace.textDocuments.find((document) => document.uri.fsPath === file);
  const kind = fileRunKind(file, open?.getText() ?? (await fs.promises.readFile(file, "utf8").catch(() => "")));
  const folder = workspaceFolderOf(file);
  if (kind === "test") {
    return startRun(context, pytestRun(`pytest ${name}`, [file], folder));
  }
  if (kind === "module") {
    const tests = await findTests(context, file, folder);
    if (tests.length) {
      return startRun(context, pytestRun(`tests for ${name}`, tests, folder));
    }
    void vscode.window.showInformationMessage(`Luciole: no test file imports ${name}, so it runs as a script.`);
  }
  return startRun(context, { name, program: file });
}

function workspaceFolderOf(file: string): string {
  return vscode.workspace.getWorkspaceFolder(vscode.Uri.file(file))?.uri.fsPath ?? path.dirname(file);
}

/** Test files under `folder` that import `file`'s module (`luciole tests`). */
function findTests(context: vscode.ExtensionContext, file: string, folder: string): Promise<string[]> {
  const dir = analyzerDir(context);
  if (!dir) {
    return Promise.resolve([]);
  }
  const args = ["-m", "luciole", "tests", file, "--root", folder, "--json"];
  return new Promise((resolve) => {
    execFile(pythonPath(file), args, { cwd: dir, env: { ...process.env, PYTHONPATH: dir } }, (error, stdout, stderr) => {
      if (error) {
        output.appendLine(`could not look for tests: ${stderr || error.message}`);
        resolve([]);
        return;
      }
      try {
        resolve(JSON.parse(stdout) as string[]);
      } catch {
        resolve([]);
      }
    });
  });
}

/** Run the last run again, else the panel's file, else ask what to run. */
function runAgain(context: vscode.ExtensionContext, panelTarget: string): Promise<void> {
  const last = context.workspaceState.get<RunConfiguration>(LAST_RUN);
  if (last) {
    return startRun(context, last);
  }
  return panelTarget.endsWith(".py") ? runFile(context, panelTarget) : pickRun(context);
}

type RunPick = vscode.QuickPickItem & { choose?: () => Promise<RunConfiguration | undefined> };

/** Ask what to run: the last run, the current file (with or without arguments), a configured run, or a module. */
async function pickRun(context: vscode.ExtensionContext): Promise<void> {
  const file = activePythonFile();
  const last = context.workspaceState.get<RunConfiguration>(LAST_RUN);
  const describe = (config: RunConfiguration) =>
    [config.module ? `-m ${config.module}` : vscode.workspace.asRelativePath(config.program ?? ""), ...(config.args ?? [])].join(" ");
  const picks: RunPick[] = [];
  if (last) {
    picks.push({ label: `$(debug-rerun) ${last.name}`, description: describe(last), detail: "Last run", choose: async () => last });
  }
  if (file) {
    picks.push(
      { label: "$(file) Current file as a script", description: vscode.workspace.asRelativePath(file), choose: async () => ({ name: path.basename(file), program: file }) },
      {
        label: "$(beaker) Tests for the current file",
        description: "pytest on the test files that import it",
        choose: async () => {
          const folder = workspaceFolderOf(file);
          const tests = await findTests(context, file, folder);
          if (!tests.length) {
            void vscode.window.showInformationMessage(`Luciole: no test file imports ${path.basename(file)}.`);
            return undefined;
          }
          return pytestRun(`tests for ${path.basename(file)}`, tests, folder);
        },
      },
      {
        label: "$(file) Current file with arguments…",
        description: vscode.workspace.asRelativePath(file),
        choose: async () => {
          const line = await vscode.window.showInputBox({ prompt: `Arguments for ${path.basename(file)}`, placeHolder: "--config configs/small.yaml" });
          return line === undefined ? undefined : { name: `${path.basename(file)} ${line}`.trim(), program: file, args: splitArgs(line) };
        },
      },
    );
  }
  const configured = vscode.workspace.getConfiguration("luciole").get<RunConfiguration[]>("runConfigurations") ?? [];
  if (configured.length) {
    picks.push({ label: "Configured", kind: vscode.QuickPickItemKind.Separator });
    picks.push(...configured.map((config) => ({ label: `$(gear) ${config.name}`, description: describe(config), choose: async () => config })));
  }
  picks.push(
    { label: "", kind: vscode.QuickPickItemKind.Separator },
    {
      label: "$(terminal) Module…",
      description: "python -m …",
      choose: async () => {
        const line = await vscode.window.showInputBox({ prompt: "Module and arguments, as after python -m", placeHolder: "pytest -x tests" });
        const [module, ...args] = splitArgs(line ?? "");
        return module ? { name: `-m ${line!.trim()}`, module, args } : undefined;
      },
    },
    {
      label: "$(settings-edit) Add a run configuration…",
      description: "luciole.runConfigurations in settings.json",
      choose: async () => {
        await vscode.commands.executeCommand("workbench.action.openWorkspaceSettingsFile");
        return undefined;
      },
    },
  );
  const picked = await vscode.window.showQuickPick(picks, { title: "Luciole: Run Live", placeHolder: "What to run under the recorder" });
  const config = await picked?.choose?.();
  if (config) {
    await startRun(context, config);
  }
}

/** Show the brain the run will light up, then start the run under the recorder in a terminal. */
async function startRun(context: vscode.ExtensionContext, config: RunConfiguration): Promise<void> {
  const dir = analyzerDir(context);
  if (!dir) {
    void vscode.window.showErrorMessage("Luciole: the analyzer is missing from the extension.");
    return;
  }
  const file = activePythonFile();
  const anchor = config.program && path.isAbsolute(config.program) ? config.program : file;
  const folder =
    (anchor && vscode.workspace.getWorkspaceFolder(vscode.Uri.file(anchor))?.uri.fsPath) ??
    vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ??
    (anchor && path.dirname(anchor));
  if (!folder) {
    void vscode.window.showErrorMessage("Luciole: open a folder or a Python file first.");
    return;
  }
  const run = resolveRun(config, { workspaceFolder: folder, file });
  if ("error" in run) {
    void vscode.window.showErrorMessage(`Luciole: ${run.error}.`);
    return;
  }
  await Promise.all(
    vscode.workspace.textDocuments.filter((d) => d.isDirty && d.languageId === "python" && d.uri.scheme === "file").map((d) => d.save()),
  );
  if (run.script) {
    if (!panel?.covers(run.script)) {
      showGraph(context, run.script, true); // a folder graph that includes the script stays, to light up the whole project
    }
  } else if (!panel?.showsFolder) {
    showGraph(context, folder, false); // a module can run anything in the project
  }
  void context.workspaceState.update(LAST_RUN, config);
  panel?.post({ type: "liveStarted", script: run.label });
  await live.run(run, folder, pythonPath(folder), dir);
}

function activePythonFile(): string | undefined {
  const document = vscode.window.activeTextEditor?.document;
  return document?.languageId === "python" && document.uri.scheme === "file" ? document.uri.fsPath : undefined;
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

  get showsFolder(): boolean {
    return !this.target.endsWith(".py");
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
      void runAgain(this.context, this.target);
    } else if (message.type === "runLiveWith") {
      void pickRun(this.context);
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

/**
 * Folder holding the `luciole` package: python/ in a packaged extension, ../src in this repo. The
 * development window (F5) always uses ../src: a python/ left by packaging may be out of date.
 */
function analyzerDir(context: vscode.ExtensionContext): string | undefined {
  const packaged = path.join(context.extensionPath, "python");
  const source = path.join(context.extensionPath, "..", "src");
  const order = context.extensionMode === vscode.ExtensionMode.Development ? [source, packaged] : [packaged, source];
  return order.find((dir) => fs.existsSync(path.join(dir, "luciole", "analyzer.py")));
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
