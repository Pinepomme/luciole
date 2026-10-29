/**
 * Live mode: this window as a viewer of the Luciole engine, which every recording Python process
 * connects to. Run Live starts the program under VS Code's Python debugger with the auto-start
 * variables, so pausing, stepping, breakpoints and variables are the debugger's and the recorder
 * lights up the brain; without the Python debugger, it runs under `luciole run` in a VS Code
 * terminal (lighting only). With auto-start on, any Python program started in a terminal connects
 * by itself, and so do the children of any of them. The engine passes on each process's frames,
 * numbered. Only processes of this window's folders are shown.
 */

import * as net from "net";
import * as path from "path";
import * as vscode from "vscode";
import { PROTOCOL, ensureEngine, parseEngineLine, viewerRoots } from "./engine";
import type { LiveFrame, LiveState } from "./messages";
import { ProcessInfo, Processes, processLabel } from "./processes";
import type { PythonDebugConfiguration, ResolvedRun } from "./runs";

export interface LiveListener {
  frame(frame: LiveFrame): void;
  state(state: LiveState): void;
  /** A process ran project code for the first time. */
  started(label: string): void;
}

export interface EngineOptions {
  python: () => string;
  analyzerDir: () => string | undefined;
  version: string;
}

const IDLE: LiveState = { running: false, stopping: false, debugged: false, withoutDebugger: false };

/** What the engine sends: its handshake, processes coming and going, and their messages. */
type EngineMessage = LiveFrame & {
  welcome?: { protocol: number; version: string };
  refused?: string;
  process?: ProcessInfo & { id: number; root?: string };
  exit?: number;
  from?: number;
};

/** The debug session of a Run Live: the marker in its configuration tells it from others. */
function isLucioleSession(session: vscode.DebugSession): boolean {
  return session.configuration.__luciole === true && !session.parentSession;
}

export class LiveSession implements vscode.Disposable {
  private engine: Promise<{ port: number; socket: net.Socket }> | undefined;
  private terminal: vscode.Terminal | undefined;
  /** A terminal Run Live's process, from its start until it connects (or its terminal closes). */
  private launching: { pid: Promise<number | undefined> } | undefined;
  /** A debugger Run Live, from the call that starts it until its session ends. */
  private debugRun: { session?: vscode.DebugSession } | undefined;
  private readonly processes = new Processes<number>();
  private state: LiveState = IDLE;
  private readonly subscriptions: vscode.Disposable[];
  private disposed = false;

  constructor(
    private readonly listener: LiveListener,
    private readonly log: vscode.OutputChannel,
    private readonly options: EngineOptions,
  ) {
    this.subscriptions = [
      vscode.window.onDidCloseTerminal((terminal) => {
        if (terminal === this.terminal) {
          this.terminal = undefined;
          this.launching = undefined;
          this.update();
        }
      }),
      vscode.debug.onDidStartDebugSession((session) => {
        if (this.debugRun && !this.debugRun.session && isLucioleSession(session)) {
          this.debugRun.session = session;
        }
      }),
      vscode.debug.onDidTerminateDebugSession((session) => {
        if (session === this.debugRun?.session) {
          this.debugRun = undefined;
          this.update();
        }
      }),
      vscode.workspace.onDidChangeWorkspaceFolders(() => this.resubscribe()),
    ];
  }

  get current(): LiveState {
    return this.state;
  }

  /** Port of the engine this window watches; finds or starts the engine and subscribes on the first call. */
  async connect(): Promise<number> {
    if (!this.engine) {
      this.engine = this.subscribe();
      this.engine.catch(() => (this.engine = undefined)); // try again next time
    }
    return (await this.engine).port;
  }

  private async subscribe(): Promise<{ port: number; socket: net.Socket }> {
    const dir = this.options.analyzerDir();
    if (!dir) {
      throw new Error("the analyzer is missing from the extension");
    }
    const port = await ensureEngine(this.options.python(), dir, (line) => this.log.appendLine(line));
    const socket = await new Promise<net.Socket>((resolve, reject) => {
      const socket: net.Socket = net.connect(port, "127.0.0.1", () => resolve(socket));
      socket.on("error", reject);
    });
    const roots = viewerRoots((vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath));
    socket.write(JSON.stringify({ hello: { role: "viewer", protocol: PROTOCOL, version: this.options.version, name: "vscode", roots } }) + "\n");
    this.read(socket);
    this.log.appendLine(`watching the Luciole engine on port ${port}`);
    return { port, socket };
  }

  /**
   * Folders were added to or removed from the workspace: the engine takes a viewer's roots only in
   * its hello, so subscribe again with the new ones. The engine announces its processes to the new
   * subscription, and their next frames light the view up again.
   */
  private resubscribe(): void {
    const socket = this.subscribed;
    if (!socket) {
      return; // not subscribed (yet): the next subscription sends the current folders
    }
    this.log.appendLine("workspace folders changed; subscribing to the Luciole engine again");
    this.subscribed = undefined;
    this.engine = undefined;
    socket.destroy();
    this.forgetProcesses();
    void this.connect().catch((error) => this.log.appendLine(`${error}`));
  }

  /**
   * Starts `config` under VS Code's Python debugger, once the engine runs (the program's
   * auto-start looks for it as it starts), ending the previous Run Live first.
   */
  async debug(config: PythonDebugConfiguration, folder: vscode.WorkspaceFolder | undefined): Promise<void> {
    try {
      await this.connect();
    } catch (error) {
      void vscode.window.showErrorMessage(`Luciole: ${error instanceof Error ? error.message : error}`);
      return;
    }
    await this.endPrevious();
    const run: { session?: vscode.DebugSession } = {};
    this.debugRun = run;
    this.setState({ ...IDLE, running: true, debugged: true });
    this.log.appendLine(`debugging ${config.module ? `-m ${config.module}` : config.program} ${config.args.join(" ")}`.trimEnd());
    const started = await vscode.debug.startDebugging(folder, config);
    if (!started && this.debugRun === run) {
      this.log.appendLine("the Python debugger did not start");
      this.debugRun = undefined;
      this.update();
    }
  }

  /** Starts `run` under `luciole run` in a fresh terminal, ending the previous Run Live first. */
  async run(run: ResolvedRun, root: string, python: string, analyzerDir: string): Promise<void> {
    let port: number;
    try {
      port = await this.connect();
    } catch (error) {
      void vscode.window.showErrorMessage(`Luciole: ${error instanceof Error ? error.message : error}`);
      return;
    }
    await this.endPrevious();
    const args = ["-m", "luciole", "run", "--root", root, "--port", String(port), ...run.target];
    // Python is the terminal's process, with no shell: other extensions (venv auto-activation)
    // type into new shells, and their Ctrl+C would kill the run.
    this.terminal = vscode.window.createTerminal({
      name: "Luciole live",
      cwd: run.cwd,
      shellPath: python,
      shellArgs: args,
      env: {
        ...run.env,
        PYTHONPATH: [analyzerDir, run.env.PYTHONPATH ?? process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      },
    });
    this.launching = { pid: Promise.resolve(this.terminal.processId) };
    this.terminal.show(true);
    this.log.appendLine(`$ ${shellCommand([python, ...args])}`);
    this.setState({ ...IDLE, running: true, withoutDebugger: true }); // only Run Live without the Python debugger runs here
  }

  /** Ends the previous Run Live: its debug session, or its terminal (its process exits, and leaves the engine with it). */
  private async endPrevious(): Promise<void> {
    const session = this.debugRun?.session;
    this.debugRun = undefined;
    if (session) {
      await vscode.debug.stopDebugging(session);
    }
    this.terminal?.dispose();
    this.terminal = undefined;
    this.launching = undefined;
  }

  /**
   * Stops what runs. A debug session (Run Live's, else the Python one being debugged) is stopped
   * by the debugger. Otherwise the first call sends SIGINT, like Ctrl+C in the terminal: the
   * program gets a KeyboardInterrupt, its `finally` blocks run and the traceback shows where it
   * stopped. A program can survive that (it catches the interrupt, or non-daemon threads keep it
   * alive), so the next call kills it.
   */
  async stop(session?: vscode.DebugSession): Promise<void> {
    const debugged = this.debugRun?.session ?? session;
    if (debugged) {
      this.setState({ ...this.state, stopping: true });
      await vscode.debug.stopDebugging(debugged);
      return;
    }
    const pids = new Set(this.processes.active.map((id) => this.processes.info(id)?.pid));
    pids.add(await this.launching?.pid);
    pids.delete(undefined);
    const signal = this.state.stopping ? "SIGKILL" : "SIGINT";
    let sent = false;
    for (const pid of pids) {
      sent = this.signal(pid!, signal) || sent;
    }
    if (sent) {
      this.setState({ ...this.state, stopping: true });
    }
  }

  /** The connection to the engine, once subscribed. */
  private subscribed: net.Socket | undefined;

  private signal(pid: number, signal: NodeJS.Signals): boolean {
    // Run Live's Python is the terminal's process (no shell in between), so this pid is the program's.
    try {
      process.kill(pid, signal);
      this.log.appendLine(`sent ${signal} to pid ${pid}`);
      return true;
    } catch (error) {
      this.log.appendLine(`could not send ${signal} to pid ${pid}: ${error}`);
      return false;
    }
  }

  dispose(): void {
    this.disposed = true;
    this.subscriptions.forEach((subscription) => subscription.dispose());
    this.subscribed?.destroy();
    this.terminal?.dispose();
  }

  /** Running while Run Live starts or its debug session lasts, or while a connected process runs project code. */
  private update(): void {
    const running = this.launching !== undefined || this.debugRun !== undefined || this.processes.active.length > 0;
    if (running && !this.state.running) {
      // A program started by the user's own debug session, with auto-start on, is under the debugger too.
      this.setState({ ...IDLE, running: true, debugged: this.debugRun !== undefined || vscode.debug.activeDebugSession !== undefined });
    } else if (!running && this.state.running) {
      this.listener.frame({ done: true });
      this.setState(IDLE);
    }
  }

  private setState(state: LiveState): void {
    this.state = state;
    void vscode.commands.executeCommand("setContext", "luciole.liveRunning", state.running);
    this.listener.state(state);
  }

  private async receive(message: EngineMessage): Promise<void> {
    if (message.welcome) {
      return;
    }
    if (message.refused) {
      this.log.appendLine(`the Luciole engine refused this window: ${message.refused}`);
      void vscode.window.showErrorMessage(`Luciole: the engine refused this window: ${message.refused}`);
      return;
    }
    if (message.process) {
      const { id, ...info } = message.process;
      this.processes.add(id);
      this.processes.identify(id, info);
      this.log.appendLine(`connected: ${info.argv.join(" ")} (pid ${info.pid})`);
      if (this.launching && info.pid === (await this.launching.pid)) {
        this.launching = undefined; // Run Live's process: running until it exits, project code or not
        this.processes.activate(id);
        this.update();
      }
      return;
    }
    if (message.exit !== undefined) {
      this.processes.remove(message.exit);
      this.update();
      return;
    }
    const id = message.from;
    if (id === undefined) {
      return;
    }
    if (message.t === undefined && !message.done) {
      return; // a reply to another engine client's command (paused, children): not for this view
    }
    const { t, stacks, calls, links, spawns, waiting, done } = message;
    const wasActive = this.processes.isActive(id);
    const frame = this.processes.merge(id, { t, stacks, calls, links, spawns, waiting, done });
    if (frame) {
      if (!wasActive) {
        this.listener.started(processLabel(this.processes.info(id)));
        this.update();
      }
      this.listener.frame(frame);
    }
  }

  /** No engine to hear from: no process runs as far as this window knows. */
  private forgetProcesses(): void {
    for (const id of this.processes.ids) {
      this.processes.remove(id);
    }
    this.update();
  }

  /** The engine's messages, one JSON object per line, handled in order. */
  private read(socket: net.Socket): void {
    this.subscribed = socket;
    socket.setEncoding("utf8");
    let buffer = "";
    // In order, though identifying Run Live's process waits for its terminal's pid.
    let handled = Promise.resolve();
    socket.on("data", (chunk: string) => {
      buffer += chunk;
      for (let newline = buffer.indexOf("\n"); newline >= 0; newline = buffer.indexOf("\n")) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        const parsed = parseEngineLine(line);
        if (!parsed || parsed.recovered) {
          this.log.appendLine(`${parsed ? "kept the end of" : "ignored"} a malformed live event: ${line.slice(0, 200)}`);
        }
        if (!parsed) {
          continue;
        }
        const message = parsed.message as EngineMessage;
        handled = handled.then(() => this.receive(message)).catch((error) => this.log.appendLine(`live event failed: ${error}`));
      }
    });
    // The engine went away (it crashed, or was killed): forget its processes, find or start an
    // engine again.
    socket.on("close", () => {
      void handled.then(() => {
        if (this.subscribed !== socket) {
          return;
        }
        this.subscribed = undefined;
        this.engine = undefined;
        this.forgetProcesses();
        if (!this.disposed) {
          this.log.appendLine("lost the Luciole engine; reconnecting");
          setTimeout(() => void this.connect().catch((error) => this.log.appendLine(`${error}`)), 2000);
        }
      });
    });
    socket.on("error", () => undefined);
  }
}

/** Quote arguments for the terminal's shell (POSIX shells, or PowerShell on Windows). */
function shellCommand(args: string[]): string {
  if (process.platform === "win32") {
    return "& " + args.map((arg) => `"${arg.replace(/"/g, '`"')}"`).join(" ");
  }
  return args.map((arg) => (/^[\w@%+=:,./-]+$/.test(arg) ? arg : `'${arg.replace(/'/g, "'\\''")}'`)).join(" ");
}
