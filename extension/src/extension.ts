/** Extension host: runs the Python analyzer, shows the graph in a webview panel, and runs live mode. */

import { ChildProcess, execFile } from "child_process";
import type { HistoryItem } from "./calls";
import { CallsView } from "./callsview";
import { randomBytes } from "crypto";
import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { addBreakpoint, hasPythonDebugger, installPythonDebugger, pythonDebugSession, registerDebugging, removeBreakpoints, sourceBreakpoints } from "./debugging";
import type { Graph } from "./graph";
import { webviewHtml } from "./html";
import { LiveSession } from "./live";
import type { FromWebview, ToWebview } from "./messages";
import type { MapEntry } from "./map";
import type { ProcessItem } from "./running";
import { RunConfiguration, debugConfiguration, fileRunKind, packageModule, pytestRun, resolveRun, splitArgs } from "./runs";
import { ActionsView, MapView, RunningView } from "./sidebar";
import { NO_FILTERS } from "./webview/filters";
import { BrainFilters } from "./focus";

let panel: GraphPanel | undefined;
let output: vscode.OutputChannel;
let live: LiveSession;
let autoStartItem: vscode.StatusBarItem;
let map: MapView;
let actions: ActionsView;
let calls: CallsView;
let filters: BrainFilters;

/** The last run started, to run it again from the panel. */
const LAST_RUN = "luciole.lastRun";
/** Run Live ran in a terminal for want of the Python debugger: the panel offers it until it is installed. */
let debuggerNotice = false;

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("Luciole");
  live = new LiveSession(
    {
      frame: (frame) => {
        panel?.post({ type: "live", frame });
        map.setRunning(frame.done ? [] : (frame.stacks ?? []).flatMap(({ stack }) => stack));
      },
      state: (state) => panel?.post({ type: "liveState", state }),
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
  actions = new ActionsView();
  map = new MapView();
  map.view = vscode.window.createTreeView("luciole.map", { treeDataProvider: map, showCollapseAll: true });
  const running = new RunningView(live);
  filters = new BrainFilters((message) => panel?.post(message), () => map.functions);
  const workspacePython = () => pythonPath(vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd());
  calls = new CallsView(
    {
      roots: () => (vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath),
      focus: (root, node) => {
        // The graph shown has it, else the run's whole folder.
        if (!panel || (!map.locate(node) && !panel.showsFolder)) {
          showGraph(context, root, false);
        }
        panel?.focusNode(node);
      },
      replay: (root, file, label, start, end) => {
        if (!panel?.showsFolder) {
          showGraph(context, root, false);
        }
        panel?.replay(file, label, start, end);
      },
      locate: (node) => map.locate(node),
      log: (line) => output.appendLine(line),
    },
    workspacePython,
    () => analyzerDir(context),
  );
  calls.view = vscode.window.createTreeView("luciole.calls", { treeDataProvider: calls, showCollapseAll: true });
  context.subscriptions.push(
    actions,
    running,
    map,
    vscode.window.createTreeView("luciole.actions", { treeDataProvider: actions }),
    vscode.window.createTreeView("luciole.running", { treeDataProvider: running }),
    map.view,
    calls,
    calls.view,
    vscode.commands.registerCommand("luciole.refreshCalls", () => calls.refresh()),
    vscode.commands.registerCommand("luciole.showCall", (item: HistoryItem) => calls.showCall(item)),
    vscode.commands.registerCommand("luciole.loadMore", (item: HistoryItem) => calls.loadMore(item)),
    vscode.commands.registerCommand("luciole.showValues", (item: HistoryItem) => calls.showValues(item)),
    vscode.commands.registerCommand("luciole.replay", (item?: HistoryItem) => calls.replay(item)),
    vscode.commands.registerCommand("luciole.focusAround", (target?: MapEntry | HistoryItem) => filters.focusAround(target)),
    vscode.commands.registerCommand("luciole.filters", () => filters.pick()),
    vscode.commands.registerCommand("luciole.clearFilters", () => filters.set(NO_FILTERS)),
    vscode.commands.registerCommand("luciole.revealInBrain", (entry: MapEntry) => panel?.focus(entry)),
    vscode.commands.registerCommand("luciole.goToSource", (entry: MapEntry) => {
      if (map.root && entry.file) {
        openSource(map.root, entry.file, entry.line ?? 1, entry.col ?? 0, false);
      }
    }),
    vscode.commands.registerCommand("luciole.findInMap", () => findInMap()),
    vscode.commands.registerCommand("luciole.stopProcess", (process: ProcessItem) => live.stopProcess(process.id)),
    vscode.commands.registerCommand("luciole.showCallStack", () => vscode.commands.executeCommand("workbench.debug.action.focusCallStackView")),
    vscode.commands.registerCommand("luciole.clearExited", () => live.clearExited()),
    output,
    live,
    vscode.commands.registerCommand("luciole.runLive", () => runFile(context, fileTarget())),
    vscode.commands.registerCommand("luciole.runLiveWith", () => pickRun(context)),
    vscode.commands.registerCommand("luciole.stopLive", () => live.stop(pythonDebugSession())),
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
        actions.refresh();
      }
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => applyAutoStart(context)),
    ...registerDebugging({
      autoStartEnv: async (folder) => {
        const dir = analyzerDir(context);
        if (!dir || !vscode.workspace.getConfiguration("luciole").get<boolean>("autoStart", false)) {
          return undefined;
        }
        await live.connect().catch((error) => output.appendLine(`Luciole engine: ${error}`));
        const root = folder?.uri.fsPath ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        return { PYTHONPATH: path.join(dir, "luciole", "_autostart"), ...(root ? { LUCIOLE_ROOT: root } : {}) };
      },
      stopped: (file, line) => {
        if (panel) {
          panel.debugStopped(file, line);
        } else {
          output.appendLine(`debugger stopped at ${file}:${line}; no graph is open to show it`);
        }
      },
      continued: () => panel?.post({ type: "debugStopped", at: undefined }),
      process: (session, pid) => live.debuggedProcess(session, pid),
      exited: (session, code) => calls.exited(session, code),
      log: (line) => output.appendLine(line),
    }),
    vscode.debug.onDidChangeBreakpoints(() => panel?.postBreakpoints()),
    // The Python debugger installed (from the panel's notice or elsewhere): the next Run Live uses it.
    vscode.extensions.onDidChange(() => {
      if (debuggerNotice && hasPythonDebugger()) {
        showDebuggerNotice(false);
      }
    }),
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
 * script as itself (as a module when it is in a package and imports relatively), a library
 * module through the tests that import it, else as a script.
 */
async function runFile(context: vscode.ExtensionContext, file: string | undefined): Promise<void> {
  if (!file) {
    return;
  }
  const name = path.basename(file);
  const open = vscode.workspace.textDocuments.find((document) => document.uri.fsPath === file);
  const source = open?.getText() ?? (await fs.promises.readFile(file, "utf8").catch(() => ""));
  const kind = fileRunKind(file, source);
  const folder = workspaceFolderOf(file);
  if (kind === "test") {
    return startRun(context, pytestRun(`pytest ${name}`, [file], folder));
  }
  if (kind === "module") {
    const tests = await findTests(context, file, folder);
    if (tests.length) {
      return startRun(context, pytestRun(`tests for ${name}`, tests, folder));
    }
  }
  // A file of a package that imports relatively only runs as a module: `python -m pkg.mod`.
  const asModule = packageModule(file, source, (dir) => fs.existsSync(path.join(dir, "__init__.py")));
  if (kind === "module") {
    const how = asModule ? `as a module (-m ${asModule.module})` : "as a script";
    void vscode.window.showInformationMessage(`Luciole: no test file imports ${name}, so it runs ${how}.`);
  }
  if (asModule) {
    // Importable from the folder it runs in; else from its package's folder, put first on PYTHONPATH.
    const env = path.resolve(asModule.root) === path.resolve(folder) ? undefined : { PYTHONPATH: [asModule.root, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) };
    return startRun(context, { name: `-m ${asModule.module}`, module: asModule.module, folder, ...(env ? { env } : {}) });
  }
  return startRun(context, { name, program: file, folder });
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
      { label: "$(file) Current file as a script", description: vscode.workspace.asRelativePath(file), choose: async () => ({ name: path.basename(file), program: file, folder: workspaceFolderOf(file) }) },
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
          return line === undefined ? undefined : { name: `${path.basename(file)} ${line}`.trim(), program: file, args: splitArgs(line), folder: workspaceFolderOf(file) };
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

/**
 * Show the brain the run will light up, then start the run under VS Code's Python debugger with
 * the recorder (auto-start), or, without the Python debugger, under `luciole run` in a terminal.
 */
async function startRun(context: vscode.ExtensionContext, config: RunConfiguration): Promise<void> {
  const dir = analyzerDir(context);
  if (!dir) {
    void vscode.window.showErrorMessage("Luciole: the analyzer is missing from the extension.");
    return;
  }
  const file = activePythonFile();
  const anchor = config.program && path.isAbsolute(config.program) ? config.program : file;
  // The run's own folder first: the panel's Run live button leaves no active editor, and in a
  // multi-root workspace the first folder may not be the one its paths are relative to.
  const folder =
    config.folder ??
    (anchor && vscode.workspace.getWorkspaceFolder(vscode.Uri.file(anchor))?.uri.fsPath) ??
    vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ??
    (anchor && path.dirname(anchor));
  if (!folder) {
    void vscode.window.showErrorMessage("Luciole: open a folder or a Python file first.");
    return;
  }
  const resolved = resolveRun(config, { workspaceFolder: folder, file });
  if ("error" in resolved) {
    void vscode.window.showErrorMessage(`Luciole: ${resolved.error}.`);
    return;
  }
  // Recorded at level 1 into a run folder (as agents' runs are): the Calls view lists it.
  const recording = await calls.record(folder, resolved);
  const run = recording ? { ...resolved, env: { ...resolved.env, ...recording.env } } : resolved;
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
  void context.workspaceState.update(LAST_RUN, { ...config, folder }); // runs again from the same folder
  panel?.post({ type: "liveStarted", script: run.label });
  if (!hasPythonDebugger()) {
    showDebuggerNotice(true);
    if (recording) {
      calls.recordingInTerminal(recording.folder);
    }
    await live.run(run, folder, pythonPath(folder), dir);
    return;
  }
  const launch = debugConfiguration(run, {
    autoStartDir: path.join(dir, "luciole", "_autostart"),
    root: folder,
    python: configuredPython(folder),
    pythonPath: process.env.PYTHONPATH,
  });
  if (recording) {
    launch.__lucioleRun = recording.folder;
  }
  await live.debug(launch, vscode.workspace.getWorkspaceFolder(vscode.Uri.file(folder)));
}

function showDebuggerNotice(show: boolean): void {
  debuggerNotice = show;
  panel?.post({ type: "debuggerNotice", show });
}

function activePythonFile(): string | undefined {
  const document = vscode.window.activeTextEditor?.document;
  return document?.languageId === "python" && document.uri.scheme === "file" ? document.uri.fsPath : undefined;
}

/** Lists the functions of the graph in the panel; the one chosen is shown in the Map and in the brain. */
async function findInMap(): Promise<void> {
  const functions = map.functions;
  if (!functions.length) {
    void vscode.window.showInformationMessage("Luciole: show a graph first.");
    return;
  }
  const picked = await vscode.window.showQuickPick(
    functions.map((entry) => ({ label: `$(symbol-function) ${entry.label}`, description: entry.container, entry })),
    { title: "Luciole: Find in Map", placeHolder: "Function to show in the brain", matchOnDescription: true },
  );
  if (picked) {
    await map.view?.reveal(picked.entry, { select: true, focus: false, expand: true });
    panel?.focus(picked.entry);
  }
}

/** Opens `file` (relative to `root`) at `line` (1-based) and `col` in the first editor column. */
function openSource(root: string, file: string, line: number, col: number, preserveFocus: boolean): void {
  const position = new vscode.Position(line - 1, col);
  void vscode.window.showTextDocument(vscode.Uri.file(path.resolve(root, file)), {
    viewColumn: vscode.ViewColumn.One,
    preserveFocus,
    selection: new vscode.Range(position, position),
  });
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
  /** Messages for the brain once its graph is shown (a focus, a replay asked for while it loads). */
  private pending: ToWebview[] = [];
  private loaded = false;
  /** The trace being replayed: the timeline's requests for frames go to it. */
  private replayFile: string | undefined;

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
      map.setGraph(undefined);
      void vscode.commands.executeCommand("setContext", "luciole.graphOpen", false);
    });
    void vscode.commands.executeCommand("setContext", "luciole.graphOpen", true);
  }

  /** Shows a Map item in the brain: the camera flies to its neuron or region, which is marked. */
  focus(entry: MapEntry): void {
    this.panel.reveal(undefined, true);
    this.post(entry.kind === "function" ? { type: "focus", node: entry.target } : { type: "focusRegion", region: entry.target });
  }

  /** Fly to the neuron `node` and ring it, once the graph is shown. */
  focusNode(node: string): void {
    this.panel.reveal(undefined, true);
    this.postWhenLoaded({ type: "focus", node });
  }

  /** Replay the process traced in `file` on the brain, from `start` to `end` (ns). */
  replay(file: string, label: string, start: number, end: number): void {
    this.replayFile = file;
    this.panel.reveal(undefined, true);
    this.postWhenLoaded({ type: "replayStart", label, start, end });
  }

  private postWhenLoaded(message: ToWebview): void {
    if (this.loaded) {
      this.post(message);
    } else {
      this.pending.push(message);
    }
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
  /** `file` relative to the graph's root, as the graph writes it; undefined outside it. */
  private relative(file: string): string | undefined {
    const relative = this.root ? path.relative(this.root, file) : "..";
    return relative.startsWith("..") || path.isAbsolute(relative) ? undefined : relative.split(path.sep).join("/");
  }

  /** VS Code's breakpoints in this graph's files, for the brain to mark their functions. */
  postBreakpoints(): void {
    const breakpoints = sourceBreakpoints().flatMap(({ file, line }) => {
      const relative = this.relative(file);
      return relative ? [{ file: relative, line }] : [];
    });
    this.post({ type: "breakpoints", breakpoints });
  }

  /** The debugger stopped at `line` of `file`: show its function, if it is in this graph. */
  debugStopped(file: string, line: number): void {
    const relative = this.relative(file);
    output.appendLine(
      relative
        ? `debugger stopped at ${relative}:${line}, shown in the brain`
        : `debugger stopped at ${file}:${line}, outside this graph (${this.root ?? "not analyzed yet"})`,
    );
    this.post({ type: "debugStopped", at: relative ? { file: relative, line } : undefined });
  }

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
      this.post({ type: "debuggerNotice", show: debuggerNotice });
      this.analyze();
    } else if (message.type === "refresh") {
      this.analyze();
    } else if (message.type === "runLive") {
      void runAgain(this.context, this.target);
    } else if (message.type === "runLiveWith") {
      void pickRun(this.context);
    } else if (message.type === "installDebugger") {
      void installPythonDebugger();
    } else if (message.type === "stopLive") {
      void live.stop(pythonDebugSession());
    } else if (message.type === "addBreakpoint" && this.root) {
      addBreakpoint(path.resolve(this.root, message.file), message.line);
    } else if (message.type === "removeBreakpoints" && this.root) {
      removeBreakpoints(path.resolve(this.root, message.file), message.lines);
    } else if (message.type === "reveal" && this.root) {
      openSource(this.root, message.file, message.line, message.col, true);
    } else if (message.type === "replayFetch" && this.replayFile) {
      const { seq, from, to, every } = message;
      calls.frames(this.replayFile, from, to, every).then(
        (frames) => this.post({ type: "replayFrames", seq, frames }),
        (error: Error) => this.post({ type: "replayFailed", seq, message: error.message }),
      );
    } else if (message.type === "replayLatest") {
      calls.replay(undefined);
    } else if (message.type === "replayClosed") {
      this.replayFile = undefined;
    } else if (message.type === "clearFilters") {
      filters.set(NO_FILTERS);
    }
  }

  analyze(): void {
    if (!this.ready) {
      return; // the webview's "ready" message starts the first run
    }
    this.running?.kill();
    this.loaded = false;
    this.post({ type: "loading", target: path.basename(this.target) });
    map.loading(path.basename(this.target));
    this.running = runAnalyzer(this.context, this.target, (result) => {
      this.running = undefined;
      if ("error" in result) {
        this.post({ type: "error", message: result.error });
        map.setGraph(undefined);
        map.failed();
      } else {
        this.root = result.graph.root;
        this.post({ type: "graph", target: this.target, graph: result.graph, warnings: result.warnings });
        map.setGraph(result.graph, path.basename(this.target));
        this.postBreakpoints();
        if (filters.active) {
          this.post({ type: "filters", filters: filters.current });
        }
        this.loaded = true;
        this.pending.splice(0).forEach((pending) => this.post(pending));
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
  return configuredPython(target) ?? (process.platform === "win32" ? "python" : "python3");
}

/** The configured interpreter, else the workspace's virtual environment; undefined when neither is there. */
function configuredPython(target: string): string | undefined {
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
  return undefined;
}
