/**
 * Live mode: this window as a viewer of the Luciole engine, which every recording Python process
 * connects to. Run Live starts one under `luciole run` in a VS Code terminal (so its output and
 * input stay usable); with auto-start on, any Python program started in a terminal connects by
 * itself, and so do the children of either. The engine passes on each process's frames, numbered,
 * and this window's commands back to them: pause, resume, step, and expand a value of the paused
 * program. Only processes of this window's folders are shown.
 */

import * as net from "net";
import * as path from "path";
import * as vscode from "vscode";
import { PROTOCOL, ensureEngine } from "./engine";
import type { LiveFrame, LiveState, PausedThread, RecorderReply, StepMode, Variable } from "./messages";
import { Handles, ProcessInfo, Processes, processLabel } from "./processes";
import type { ResolvedRun } from "./runs";

export interface LiveListener {
  frame(frame: LiveFrame): void;
  state(state: LiveState): void;
  /** A thread stopped after a pause; all of them are resumed together. */
  paused(thread: PausedThread): void;
  /** A process ran project code for the first time. */
  started(label: string): void;
}

export interface EngineOptions {
  python: () => string;
  analyzerDir: () => string | undefined;
  version: string;
}

const IDLE: LiveState = { running: false, paused: false, held: 0, stepping: false, stopping: false };

/** What the engine sends: its handshake, processes coming and going, and their messages. */
type EngineMessage = LiveFrame &
  RecorderReply & {
    welcome?: { protocol: number; version: string };
    refused?: string;
    process?: ProcessInfo & { id: number; root?: string };
    exit?: number;
    from?: number;
  };

export class LiveSession implements vscode.Disposable {
  private engine: Promise<{ port: number; socket: net.Socket }> | undefined;
  private terminal: vscode.Terminal | undefined;
  /** Run Live's process, from its start until it connects (or its terminal closes). */
  private launching: { pid: Promise<number | undefined> } | undefined;
  private readonly processes = new Processes<number>();
  /** Thread idents and value refs of all processes, as numbers unique across them. */
  private readonly threads = new Handles<number>();
  private readonly refs = new Handles<number>();
  private state: LiveState = IDLE;
  /** Pending "expand" requests, by ref handle. */
  private readonly expanding = new Map<number, (vars: Variable[]) => void>();
  private readonly subscription: vscode.Disposable;
  private disposed = false;

  constructor(
    private readonly listener: LiveListener,
    private readonly log: vscode.OutputChannel,
    private readonly options: EngineOptions,
  ) {
    this.subscription = vscode.window.onDidCloseTerminal((terminal) => {
      if (terminal === this.terminal) {
        this.terminal = undefined;
        this.launching = undefined;
        this.update();
      }
    });
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
    const roots = (vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath);
    socket.write(JSON.stringify({ hello: { role: "viewer", protocol: PROTOCOL, version: this.options.version, name: "vscode", roots } }) + "\n");
    this.read(socket);
    this.log.appendLine(`watching the Luciole engine on port ${port}`);
    return { port, socket };
  }

  /** Starts `run` in a fresh terminal, closing the previous Run Live terminal. */
  async run(run: ResolvedRun, root: string, python: string, analyzerDir: string): Promise<void> {
    let port: number;
    try {
      port = await this.connect();
    } catch (error) {
      void vscode.window.showErrorMessage(`Luciole: ${error instanceof Error ? error.message : error}`);
      return;
    }
    this.terminal?.dispose(); // its process exits, and leaves the engine with it
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
    if (this.state.paused) {
      this.pause(false); // other processes paused with the previous run go on
    }
    this.setState({ ...IDLE, running: true });
  }

  /**
   * Puts every running process on hold, or resumes them. Each thread stops at its next line of
   * project code and sends its variables; a thread busy in library or C code (a sleep, a long
   * torch call) stops when it gets back to project code.
   */
  pause(paused: boolean): void {
    if (paused === this.state.paused || !this.state.running || this.state.stopping) {
      return;
    }
    if (!this.command({ cmd: paused ? "pause" : "resume" })) {
      void vscode.window.showWarningMessage("Luciole: the program is not connected to the view yet.");
      return;
    }
    this.resolveExpanding();
    this.setState({ ...this.state, paused, held: 0, stepping: false });
  }

  /**
   * Runs the stopped `thread` to its next line ("into"), its next line in the same function
   * ("over": calls on the way run without stopping), or its caller's next line ("out"), then
   * pauses its process again. The process's other threads run meanwhile, as in VS Code's Python
   * debugger: keeping them stopped would hang a step over a `join()` or a queue read. Other
   * processes stay paused.
   */
  step(thread: number, mode: StepMode): void {
    if (!this.state.paused || !this.state.held || this.state.stepping) {
      return;
    }
    const target = this.threads.resolve(thread);
    if (target && this.command({ cmd: "step", thread: target[1], mode }, target[0])) {
      this.resolveExpanding();
      this.setState({ ...this.state, held: 0, stepping: true });
    }
  }

  /** Children of a value of the paused program (items of a container, attributes of an object). */
  expand(ref: number): Promise<Variable[]> {
    return new Promise((resolve) => {
      const target = this.refs.resolve(ref);
      if (!this.state.paused || !target || !this.command({ cmd: "expand", ref: target[1] }, target[0])) {
        resolve([]);
        return;
      }
      this.expanding.set(ref, resolve);
    });
  }

  /**
   * Stops what runs. The first call sends SIGINT, like Ctrl+C in the terminal: the program gets a
   * KeyboardInterrupt, its `finally` blocks run and the traceback shows where it stopped. A program
   * can survive that (it catches the interrupt, or non-daemon threads keep it alive), so the next
   * call kills it.
   */
  async stop(): Promise<void> {
    if (this.state.paused) {
      this.pause(false); // paused threads would keep the process alive
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
      this.setState({ ...this.state, paused: false, held: 0, stepping: false, stopping: true });
    }
  }

  /** Sends `command` to process `id` through the engine, or to every running process. False when none got it. */
  private command(command: object, id?: number): boolean {
    const socket = this.subscribed;
    const targets = id === undefined ? this.processes.active : [id];
    if (!socket || socket.destroyed || !targets.length) {
      return false;
    }
    for (const to of targets) {
      socket.write(JSON.stringify({ ...command, to }) + "\n");
    }
    return true;
  }

  /** The connection to the engine, once subscribed. */
  private subscribed: net.Socket | undefined;

  private resolveExpanding(): void {
    for (const resolve of this.expanding.values()) {
      resolve([]);
    }
    this.expanding.clear();
    this.refs.clear(); // recorders drop their refs when they resume
  }

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
    this.subscription.dispose();
    this.subscribed?.destroy();
    this.terminal?.dispose();
  }

  /** Running while Run Live starts, or while a connected process runs project code. */
  private update(): void {
    const running = this.launching !== undefined || this.processes.active.length > 0;
    if (running && !this.state.running) {
      this.setState({ ...IDLE, running: true });
    } else if (!running && this.state.running) {
      this.resolveExpanding();
      this.listener.frame({ done: true });
      this.setState(IDLE);
    }
  }

  private setState(state: LiveState): void {
    this.state = state;
    void vscode.commands.executeCommand("setContext", "luciole.liveRunning", state.running);
    void vscode.commands.executeCommand("setContext", "luciole.livePaused", state.paused);
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
      this.threads.clear(message.exit);
      this.update();
      return;
    }
    const id = message.from;
    if (id === undefined) {
      return;
    }
    if (message.paused) {
      if (this.state.paused) {
        this.setState({ ...this.state, held: this.state.held + 1, stepping: false });
        const thread = message.paused;
        const label = this.processes.active.length > 1 ? `${processLabel(this.processes.info(id))} · ` : "";
        this.listener.paused({
          thread: label + thread.thread,
          id: this.threads.handle(id, thread.id),
          frames: thread.frames.map((frame) => ({ ...frame, vars: this.mapRefs(id, frame.vars) })),
        });
      }
    } else if (message.children) {
      const ref = this.refs.handle(id, message.children.ref);
      this.expanding.get(ref)?.(this.mapRefs(id, message.children.vars));
      this.expanding.delete(ref);
    } else {
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
  }

  private mapRefs(id: number, vars: Variable[]): Variable[] {
    return vars.map((v) => (v.ref === undefined ? v : { ...v, ref: this.refs.handle(id, v.ref) }));
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
        let message: EngineMessage;
        try {
          message = JSON.parse(line) as EngineMessage;
        } catch {
          this.log.appendLine(`ignored a malformed live event: ${line.slice(0, 200)}`);
          continue;
        }
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
        for (const id of this.processes.ids) {
          this.processes.remove(id);
        }
        this.threads.clear();
        this.update();
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
