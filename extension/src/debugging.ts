/**
 * VS Code's own Python debugger (debugpy) and the brain: breakpoints, stepping, watch, hover and
 * the debug console stay the debugger's; Luciole adds three things.
 *
 * - With auto-start on, a launched program gets the auto-start variables, so it lights up the
 *   brain while you debug it.
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
  /** A line for the Luciole output, to tell why the brain did or did not follow the debugger. */
  log(line: string): void;
}

/** The running Python debug session, if any: Luciole's pause and step commands then drive it. */
export function pythonDebugSession(): vscode.DebugSession | undefined {
  const session = vscode.debug.activeDebugSession;
  return session && PYTHON_DEBUG_TYPES.includes(session.type) ? session : undefined;
}

/** VS Code's debugger command doing what a Luciole live command does. */
export const DEBUGGER_COMMANDS = {
  pause: "workbench.action.debug.pause",
  resume: "workbench.action.debug.continue",
  over: "workbench.action.debug.stepOver",
  into: "workbench.action.debug.stepInto",
  out: "workbench.action.debug.stepOut",
  stop: "workbench.action.debug.stop",
} as const;

export function registerDebugging(hooks: DebugHooks): vscode.Disposable[] {
  const provider: vscode.DebugConfigurationProvider = {
    async resolveDebugConfigurationWithSubstitutedVariables(folder, config) {
      const env = config.request === "launch" ? await hooks.autoStartEnv(folder) : undefined;
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
        onDidSendMessage(message: { type?: string; event?: string; body?: { threadId?: number } }) {
          if (message.type !== "event") {
            return;
          }
          if (message.event === "stopped") {
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
