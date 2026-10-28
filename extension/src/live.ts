/**
 * Live mode: a local socket that recorders connect to, one connection per Python process. Run
 * Live starts one under `luciole run` in a VS Code terminal (so its output and input stay usable);
 * with auto-start on, any Python program started in a terminal connects by itself, and so do the
 * children of either. The same sockets carry commands back to the recorders: pause, resume, step,
 * and expand a value of the paused program.
 */

import * as net from "net";
import * as path from "path";
import * as vscode from "vscode";
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

const IDLE: LiveState = { running: false, paused: false, held: 0, stepping: false, stopping: false };

type Message = LiveFrame & RecorderReply & { process?: ProcessInfo };

export class LiveSession implements vscode.Disposable {
  private port: Promise<number> | undefined;
  private server: net.Server | undefined;
  private terminal: vscode.Terminal | undefined;
  /** Run Live's process, from its start until it connects (or its terminal closes). */
  private launching: { pid: Promise<number | undefined> } | undefined;
  private readonly processes = new Processes<net.Socket>();
  /** Thread idents and value refs of all processes, as numbers unique across them. */
  private readonly threads = new Handles<net.Socket>();
  private readonly refs = new Handles<net.Socket>();
  private state: LiveState = IDLE;
  /** Pending "expand" requests, by ref handle. */
  private readonly expanding = new Map<number, (vars: Variable[]) => void>();
  private readonly subscription: vscode.Disposable;

  constructor(
    private readonly listener: LiveListener,
    private readonly log: vscode.OutputChannel,
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

  /** Port the recorders connect to; the view listens from the first call on. */
  listen(): Promise<number> {
    if (!this.port) {
      this.port = new Promise((resolve, reject) => {
        this.server = net.createServer((socket) => this.accept(socket));
        this.server.on("error", reject);
        this.server.listen(0, "127.0.0.1", () => resolve((this.server!.address() as net.AddressInfo).port));
      });
    }
    return this.port;
  }

  /** Starts `run` in a fresh terminal, closing the previous Run Live terminal. */
  async run(run: ResolvedRun, root: string, python: string, analyzerDir: string): Promise<void> {
    const port = await this.listen();
    this.terminal?.dispose(); // its process exits, and its connection with it
    const args = ["-m", "luciole", "run", "--root", root, ...run.target];
    // Python is the terminal's process, with no shell: other extensions (venv auto-activation)
    // type into new shells, and their Ctrl+C would kill the run.
    this.terminal = vscode.window.createTerminal({
      name: "Luciole live",
      cwd: run.cwd,
      shellPath: python,
      shellArgs: args,
      env: {
        ...run.env,
        LUCIOLE_LIVE_PORT: String(port),
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
   * Puts every connected process on hold, or resumes them. Each thread stops at its next line of
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
    const pids = new Set(this.processes.active.map((socket) => this.processes.info(socket)?.pid));
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

  /** Sends `command` to `socket`, or to every running process. False when none got it. */
  private command(command: object, socket?: net.Socket): boolean {
    const line = JSON.stringify(command) + "\n";
    let sent = false;
    for (const target of socket ? [socket] : this.processes.active) {
      if (!target.destroyed) {
        target.write(line);
        sent = true;
      }
    }
    return sent;
  }

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
    this.subscription.dispose();
    this.server?.close();
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

  private async receive(socket: net.Socket, message: Message): Promise<void> {
    if (message.process) {
      this.processes.identify(socket, message.process);
      this.log.appendLine(`connected: ${message.process.argv.join(" ")} (pid ${message.process.pid})`);
      if (this.launching && message.process.pid === (await this.launching.pid)) {
        this.launching = undefined; // Run Live's process: running until it exits, project code or not
        this.processes.activate(socket);
        this.update();
      }
    } else if (message.paused) {
      if (this.state.paused) {
        this.setState({ ...this.state, held: this.state.held + 1, stepping: false });
        const thread = message.paused;
        const label = this.processes.active.length > 1 ? `${processLabel(this.processes.info(socket))} · ` : "";
        this.listener.paused({
          thread: label + thread.thread,
          id: this.threads.handle(socket, thread.id),
          frames: thread.frames.map((frame) => ({ ...frame, vars: this.mapRefs(socket, frame.vars) })),
        });
      }
    } else if (message.children) {
      const ref = this.refs.handle(socket, message.children.ref);
      this.expanding.get(ref)?.(this.mapRefs(socket, message.children.vars));
      this.expanding.delete(ref);
    } else {
      const wasActive = this.processes.isActive(socket);
      const frame = this.processes.merge(socket, message);
      if (frame) {
        if (!wasActive) {
          this.listener.started(processLabel(this.processes.info(socket)));
          this.update();
        }
        this.listener.frame(frame);
      }
    }
  }

  private mapRefs(socket: net.Socket, vars: Variable[]): Variable[] {
    return vars.map((v) => (v.ref === undefined ? v : { ...v, ref: this.refs.handle(socket, v.ref) }));
  }

  /** One connection per process; one JSON object per line. */
  private accept(socket: net.Socket): void {
    this.processes.add(socket);
    socket.setEncoding("utf8");
    let buffer = "";
    // Messages are handled in order, though identifying Run Live's process waits for its pid.
    let handled = Promise.resolve();
    socket.on("data", (chunk: string) => {
      buffer += chunk;
      for (let newline = buffer.indexOf("\n"); newline >= 0; newline = buffer.indexOf("\n")) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        let message: Message;
        try {
          message = JSON.parse(line) as Message;
        } catch {
          this.log.appendLine(`ignored a malformed live event: ${line.slice(0, 200)}`);
          continue;
        }
        handled = handled.then(() => this.receive(socket, message)).catch((error) => this.log.appendLine(`live event failed: ${error}`));
      }
    });
    // The recorder sends {"done": true} when the program's main code ends, but threads can keep
    // the process alive after that: a process is over when it exits and its socket closes.
    socket.on("close", () => {
      void handled.then(() => {
        this.processes.remove(socket);
        this.threads.clear(socket);
        this.update();
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
