/**
 * VS Code's own Python debugger (debugpy) and the brain: breakpoints, stepping, watch, hover and
 * the debug console stay the debugger's; Luciole adds three things.
 *
 * - With auto-start on, a launched program gets the auto-start variables, so it lights up the
 *   brain while you debug it. ⚡ Run Live launches its programs under this debugger with these
 *   variables already set, switch or not (see `debugConfiguration` in runs.ts).
 * - The brain follows the debugger: when it stops, the function it stopped in is shown.
 * - Breakpoints set from the brain (right-click on a neuron) are ordinary VS Code breakpoints,
 *   in the Breakpoints view like any other; the brain marks the functions that have one.
 */

import * as path from "path";
import * as vscode from "vscode";

/** Debug types of the Python extension: "debugpy" now, "python" before. */
const PYTHON_DEBUG_TYPES = ["debugpy", "python"];

export interface DebugHooks {
  /** Variables that light up a launched program, or undefined when auto-start is off. */
  autoStartEnv(folder: vscode.WorkspaceFolder | undefined): Promise<Record<string, string> | undefined>;
  /** The debugger stopped at `line` of `file` (absolute path). */
  stopped(file: string, line: number): void;
  /** The debugger went on, or the session ended. */
  continued(): void;
  /** The session runs the process `pid` (the debug adapter's "process" event). */
  process(session: vscode.DebugSession, pid: number): void;
  /** The session's program exited with `code` (the debug adapter's "exited" event). */
  exited(session: vscode.DebugSession, code: number): void;
  /** A line for the Luciole output, to tell why the brain did or did not follow the debugger. */
  log(line: string): void;
}

/** The running Python debug session, if any: Luciole's Stop then stops it. */
export function pythonDebugSession(): vscode.DebugSession | undefined {
  const session = vscode.debug.activeDebugSession;
  return session && PYTHON_DEBUG_TYPES.includes(session.type) ? session : undefined;
}

/** True when the Python debugger (the ms-python.debugpy extension) is installed, for Run Live. */
export function hasPythonDebugger(): boolean {
  return vscode.extensions.getExtension("ms-python.debugpy") !== undefined;
}

/** Installs the Python debugger from the Marketplace, for the next Run Live. */
export async function installPythonDebugger(): Promise<void> {
  try {
    await vscode.commands.executeCommand("workbench.extensions.installExtension", "ms-python.debugpy");
    void vscode.window.showInformationMessage("Luciole: the Python Debugger is installed. The next Run Live runs under it, with breakpoints and stepping.");
  } catch (error) {
    void vscode.window.showErrorMessage(`Luciole: could not install the Python Debugger: ${error instanceof Error ? error.message : error}`);
  }
}

export function registerDebugging(hooks: DebugHooks): vscode.Disposable[] {
  const provider: vscode.DebugConfigurationProvider = {
    async resolveDebugConfigurationWithSubstitutedVariables(folder, config) {
      // Run Live's own launches carry the variables already.
      const env = config.request === "launch" && !config.__luciole ? await hooks.autoStartEnv(folder) : undefined;
      if (env) {
        const own: Record<string, string> = config.env ?? {};
        const pythonPath = [env.PYTHONPATH, own.PYTHONPATH ?? process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
        config.env = { ...own, ...env, PYTHONPATH: pythonPath };
      }
      return config;
    },
  };
  const trackers: vscode.DebugAdapterTrackerFactory = {
    createDebugAdapterTracker(session) {
      hooks.log(`debugger: following session "${session.name}" (${session.type})`);
      return {
        onDidSendMessage(message: { type?: string; event?: string; body?: { threadId?: number; systemProcessId?: number; exitCode?: number } }) {
          if (message.type !== "event") {
            return;
          }
          if (message.event === "process" && message.body?.systemProcessId !== undefined) {
            hooks.process(session, message.body.systemProcessId);
          } else if (message.event === "exited" && message.body?.exitCode !== undefined) {
            hooks.exited(session, message.body.exitCode);
          } else if (message.event === "stopped") {
            void reportStop(session, message.body?.threadId, hooks);
          } else if (message.event === "continued" || message.event === "terminated") {
            hooks.continued();
          }
        },
        onWillStopSession: () => hooks.continued(),
      };
    },
  };
  return PYTHON_DEBUG_TYPES.flatMap((type) => [
    vscode.debug.registerDebugConfigurationProvider(type, provider),
    vscode.debug.registerDebugAdapterTrackerFactory(type, trackers),
  ]);
}

/** Where the stopped thread is: the top frame of its stack, asked of the debugger. */
async function reportStop(session: vscode.DebugSession, threadId: number | undefined, hooks: DebugHooks): Promise<void> {
  if (threadId === undefined) {
    hooks.log("debugger stopped, with no thread named");
    return;
  }
  try {
    const reply = (await session.customRequest("stackTrace", { threadId, startFrame: 0, levels: 1 })) as {
      stackFrames?: { line: number; source?: { path?: string } }[];
    };
    const frame = reply.stackFrames?.[0];
    if (frame?.source?.path) {
      // In a remote window (WSL, SSH, containers), debugpy gives a URI: vscode-remote://wsl%2Bubuntu/home/…
      const where = frame.source.path;
      hooks.stopped(/^[a-z][\w+.-]*:\/\//i.test(where) ? vscode.Uri.parse(where).fsPath : where, frame.line);
    } else {
      hooks.log(`debugger stopped in thread ${threadId}, in a frame with no source file`);
    }
  } catch (error) {
    hooks.log(`debugger stopped in thread ${threadId}, but its stack could not be read: ${error}`); // the session may have ended
  }
}

function breakpointsAt(file: string, lines: number[]): vscode.SourceBreakpoint[] {
  const fsPath = vscode.Uri.file(file).fsPath;
  return vscode.debug.breakpoints.filter(
    (breakpoint): breakpoint is vscode.SourceBreakpoint =>
      breakpoint instanceof vscode.SourceBreakpoint && breakpoint.location.uri.fsPath === fsPath && lines.includes(breakpoint.location.range.start.line + 1),
  );
}

/** Adds a breakpoint at `line` (1-based) of `file`, in place of a disabled one there. */
export function addBreakpoint(file: string, line: number): void {
  const existing = breakpointsAt(file, [line]);
  if (existing.some((breakpoint) => breakpoint.enabled)) {
    return;
  }
  vscode.debug.removeBreakpoints(existing);
  vscode.debug.addBreakpoints([new vscode.SourceBreakpoint(new vscode.Location(vscode.Uri.file(file), new vscode.Position(line - 1, 0)))]);
}

/** Removes the breakpoints at `lines` (1-based) of `file`. */
export function removeBreakpoints(file: string, lines: number[]): void {
  vscode.debug.removeBreakpoints(breakpointsAt(file, lines));
}

/** Enabled source breakpoints, as absolute file paths and 1-based lines. */
export function sourceBreakpoints(): { file: string; line: number }[] {
  return vscode.debug.breakpoints
    .filter((breakpoint): breakpoint is vscode.SourceBreakpoint => breakpoint instanceof vscode.SourceBreakpoint && breakpoint.enabled)
    .map((breakpoint) => ({ file: breakpoint.location.uri.fsPath, line: breakpoint.location.range.start.line + 1 }));
}
