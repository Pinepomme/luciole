/**
 * Live mode: runs a script under `luciole run` in a VS Code terminal (so its output and input
 * stay usable), and receives the recorder's events on a local socket. The same socket carries
 * commands back to the recorder: pause, resume, and expand a value of the paused program.
 */

import * as net from "net";
import * as path from "path";
import * as vscode from "vscode";
import type { LiveFrame, LiveState, PausedThread, RecorderReply, StepMode, Variable } from "./messages";

export interface LiveListener {
  frame(frame: LiveFrame): void;
  state(state: LiveState): void;
  /** A thread stopped after a pause; all of them are resumed together. */
  paused(thread: PausedThread): void;
}

const IDLE: LiveState = { running: false, paused: false, held: 0, stepping: false, stopping: false };

export class LiveSession implements vscode.Disposable {
  private port: Promise<number> | undefined;
  private server: net.Server | undefined;
  private terminal: vscode.Terminal | undefined;
  private socket: net.Socket | undefined;
  private state: LiveState = IDLE;
  /** Pending "expand" requests, by ref. */
  private readonly expanding = new Map<number, (vars: Variable[]) => void>();
  private readonly subscription: vscode.Disposable;

  constructor(
    private readonly listener: LiveListener,
    private readonly log: vscode.OutputChannel,
  ) {
    this.subscription = vscode.window.onDidCloseTerminal((terminal) => {
      if (terminal === this.terminal) {
        this.terminal = undefined;
        this.finish();
      }
    });
  }

  get current(): LiveState {
    return this.state;
  }

  /** Runs `script` in a fresh terminal, stopping the previous run if it is still going. */
  async run(script: string, root: string, python: string, analyzerDir: string): Promise<void> {
    const port = await this.listen();
    // The previous run's connection closes when its terminal goes: that must not end this run.
    this.socket?.removeAllListeners();
    this.socket?.destroy();
    this.socket = undefined;
    this.terminal?.dispose();
    const args = ["-m", "luciole", "run", "--root", root, script];
    // Python is the terminal's process, with no shell: other extensions (venv auto-activation)
    // type into new shells, and their Ctrl+C would kill the run.
    this.terminal = vscode.window.createTerminal({
      name: "Luciole live",
      cwd: root,
      shellPath: python,
      shellArgs: args,
      env: {
        LUCIOLE_LIVE_PORT: String(port),
        PYTHONPATH: [analyzerDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      },
    });
    this.terminal.show(true);
    this.log.appendLine(`$ ${shellCommand([python, ...args])}`);
    this.setState({ ...IDLE, running: true });
  }

  /**
   * Puts the run on hold, or resumes it. Each thread stops at its next line of project code and
   * sends its variables; a thread busy in library or C code (a sleep, a long torch call) stops
   * when it gets back to project code.
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
   * pauses everything again. The other threads run meanwhile, as in VS Code's Python debugger:
   * keeping them stopped would hang a step over a `join()` or a queue read.
   */
  step(thread: number, mode: StepMode): void {
    if (!this.state.paused || !this.state.held || this.state.stepping) {
      return;
    }
    if (this.command({ cmd: "step", thread, mode })) {
      this.resolveExpanding();
      this.setState({ ...this.state, held: 0, stepping: true });
    }
  }

  /** Children of a value of the paused program (items of a container, attributes of an object). */
  expand(ref: number): Promise<Variable[]> {
    return new Promise((resolve) => {
      if (!this.state.paused || !this.command({ cmd: "expand", ref })) {
        resolve([]);
        return;
      }
      this.expanding.set(ref, resolve);
    });
  }

  /**
   * Stops the run. The first call sends SIGINT, like Ctrl+C in the terminal: the program gets a
   * KeyboardInterrupt, its `finally` blocks run and the traceback shows where it stopped. A program
   * can survive that (it catches the interrupt, or non-daemon threads keep it alive), so the next
   * call kills it.
   */
  async stop(): Promise<void> {
    if (this.state.paused) {
      this.pause(false); // paused threads would keep the process alive
    }
    if (await this.signal(this.state.stopping ? "SIGKILL" : "SIGINT")) {
      this.setState({ ...this.state, paused: false, held: 0, stepping: false, stopping: true });
    }
  }

  private command(command: object): boolean {
    if (!this.socket || this.socket.destroyed) {
      return false;
    }
    this.socket.write(JSON.stringify(command) + "\n");
    return true;
  }

  private resolveExpanding(): void {
    for (const resolve of this.expanding.values()) {
      resolve([]);
    }
    this.expanding.clear();
  }

  /** Sends `signal` to the run's process; false when there is no run or it failed. */
  private async signal(signal: NodeJS.Signals): Promise<boolean> {
    const pid = this.state.running ? await this.terminal?.processId : undefined;
    if (!pid) {
      return false;
    }
    // Python is the terminal's process (no shell in between), so this pid is the program's.
    try {
      process.kill(pid, signal);
      this.log.appendLine(`sent ${signal} to the live run (pid ${pid})`);
      return true;
    } catch (error) {
      this.log.appendLine(`could not send ${signal} to the live run (pid ${pid}): ${error}`);
      return false;
    }
  }

  dispose(): void {
    this.subscription.dispose();
    this.server?.close();
    this.terminal?.dispose();
  }

  private listen(): Promise<number> {
    if (!this.port) {
      this.port = new Promise((resolve, reject) => {
        this.server = net.createServer((socket) => this.accept(socket));
        this.server.on("error", reject);
        this.server.listen(0, "127.0.0.1", () => resolve((this.server!.address() as net.AddressInfo).port));
      });
    }
    return this.port;
  }

  /** The run is over: its process exited, or its terminal was closed. */
  private finish(): void {
    if (this.state.running) {
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

  private receive(message: LiveFrame & RecorderReply): void {
    if (message.paused) {
      if (this.state.paused) {
        this.setState({ ...this.state, held: this.state.held + 1, stepping: false });
        this.listener.paused(message.paused);
      }
    } else if (message.children) {
      this.expanding.get(message.children.ref)?.(message.children.vars);
      this.expanding.delete(message.children.ref);
    } else {
      this.listener.frame(message);
    }
  }

  /** One connection per run; one JSON object per line. */
  private accept(socket: net.Socket): void {
    this.socket = socket;
    socket.setEncoding("utf8");
    let buffer = "";
    socket.on("data", (chunk: string) => {
      buffer += chunk;
      for (let newline = buffer.indexOf("\n"); newline >= 0; newline = buffer.indexOf("\n")) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        try {
          this.receive(JSON.parse(line) as LiveFrame & RecorderReply);
        } catch {
          this.log.appendLine(`ignored a malformed live event: ${line.slice(0, 200)}`);
        }
      }
    });
    // The recorder sends {"done": true} when the program's main code ends, but threads can keep
    // the process alive after that: the run is over when the process exits and the socket closes.
    socket.on("close", () => this.finish());
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
