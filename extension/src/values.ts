/**
 * The page "Show Values" opens for one recorded call: its arguments, its return value or
 * exception, its local variables as an exception left them, and, when the run recorded the
 * function at level 2, each variable's values line by line. Read-only; a line number opens the
 * editor there. Pure logic (no VS Code): the HTML is built here, the panel in callsview.ts.
 */

import { durationText, shortNode, Snapshot, TraceCall } from "./calls";

/** `Trace.history(frame=N)` (src/luciole/trace.py). */
export interface CallHistory {
  nodes: string[];
  scope: string[] | null;
  recorded: boolean;
  total: number;
  frames: (TraceCall & { writes: { line: number; name: string; value: Snapshot | null; time_ns: number }[] })[];
}

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function value(snapshot: Snapshot | null | undefined): string {
  if (!snapshot) {
    return `<span class="dim">?</span>`;
  }
  const items = snapshot.items ? ` <span class="dim">(${snapshot.items.len} items)</span>` : "";
  return `<code>${escapeHtml(snapshot.repr)}</code> <span class="dim">${escapeHtml(snapshot.type)}</span>${items}${snapshot.truncated ? ` <span class="dim">(cut)</span>` : ""}`;
}

function table(rows: [string, string][]): string {
  return `<table>${rows.map(([name, cell]) => `<tr><th>${name}</th><td>${cell}</td></tr>`).join("")}</table>`;
}

function line(n: number | null | undefined): string {
  return n ? `<a href="#" data-line="${n}">line ${n}</a>` : "";
}

export interface ValuesPage {
  call: TraceCall;
  history: CallHistory | undefined;
  /** Where the history query failed, or why there is none. */
  problem?: string;
  /** `luciole run --scope …` for this function, to say how to record its variables. */
  scopeHint: string;
  /** Each value gets a "where from?" link (the Origin view answers). */
  askOrigin?: boolean;
}

/** The body of the page (inside the document `valuesHtml` makes). */
export function valuesBody({ call, history, problem, scopeHint, askOrigin }: ValuesPage): string {
  const parts: string[] = [];
  // "where from?" asks about `name` as it was at `at` (ns since the run started; default: the call's end).
  const from = (name: string, at?: number) =>
    askOrigin ? ` <a href="#" class="origin" data-origin="${escapeHtml(name)}"${at !== undefined ? ` data-at="${at}"` : ""} title="Where did this value come from?">where from?</a>` : "";
  const name = shortNode(call.node);
  const where = call.caller ? `called by <b>${escapeHtml(shortNode(call.caller.node ?? "?"))}</b> at <a href="#" data-caller="${call.caller.line}">line ${call.caller.line}</a>` : "a root of its thread";
  parts.push(`<h1>${escapeHtml(name)} <span class="dim">#${call.frame}</span></h1>`);
  parts.push(`<p class="dim">${escapeHtml(call.node)} · ${where} · ${durationText(call.duration_ns)} · thread ${escapeHtml(call.thread ?? "?")}</p>`);
  if (call.args === undefined) {
    parts.push(`<p>Recorded at level 0: no values. Run Live records at level 1 (setting <code>luciole.recordHistory</code>).</p>`);
  } else {
    const args = Object.entries(call.args);
    parts.push(`<h2>Arguments</h2>`, args.length ? table(args.map(([n, v]) => [escapeHtml(n), value(v) + from(n, call.start_ns)])) : `<p class="dim">none</p>`);
  }
  if (call.end === "return" && call.result !== undefined) {
    parts.push(`<h2>Returned</h2>`, `<p>${value(call.result)}${from("return")}</p>`);
  } else if ((call.end === "raise" || call.end === "unwind") && call.exception) {
    const e = call.exception;
    const how = call.end === "raise" ? "Raised" : "An exception went through it";
    parts.push(`<h2>${how}</h2>`, `<p class="error"><code>${escapeHtml(`${e.type ?? "?"}: ${e.message ?? ""}`)}</code></p>`);
  } else {
    parts.push(`<h2>Ended</h2>`, `<p>${escapeHtml(call.end ?? "still running")}</p>`);
  }
  if (call.locals && Object.keys(call.locals).length) {
    parts.push(`<h2>Local variables as the exception left them</h2>`, table(Object.entries(call.locals).map(([n, v]) => [escapeHtml(n), value(v) + from(n)])));
  }
  parts.push(`<h2>Variables over time</h2>`);
  if (problem) {
    parts.push(`<p class="dim">${escapeHtml(problem)}</p>`);
  } else if (!history?.recorded) {
    parts.push(
      `<p class="dim">Not recorded: line-by-line values are kept only for the functions a run scopes (level 2). Record them with <code>${escapeHtml(scopeHint)}</code>, or ask an agent to run it with <code>scope</code>.</p>`,
    );
  } else {
    const writes = history.frames.flatMap((frame) => frame.writes);
    if (!writes.length) {
      parts.push(`<p class="dim">No local variable was assigned in this call.</p>`);
    } else {
      const start = call.start_ns;
      const rows = writes.map(
        (w) => `<tr><td>${line(w.line)}</td><th>${escapeHtml(w.name)}</th><td>${value(w.value)}${from(w.name, w.time_ns)}</td><td class="dim">+${durationText(w.time_ns - start)}</td></tr>`,
      );
      parts.push(`<table class="history">${rows.join("")}</table>`);
      if (history.total > writes.length) {
        parts.push(`<p class="dim">${history.total - writes.length} more assignments not shown (luciole trace FILE history --frame ${call.frame} --offset ${writes.length}).</p>`);
      }
    }
  }
  return parts.join("\n");
}

/** The whole document: `nonce` for its one script, which turns line links into messages. */
export function valuesHtml(body: string, nonce: string, cspSource: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body { font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); color: var(--vscode-foreground); padding: 0 16px 16px; }
    h1 { font-size: 1.3em; font-weight: 600; }
    h2 { font-size: 1em; font-weight: 600; margin-top: 1.4em; }
    table { border-collapse: collapse; }
    th, td { text-align: left; vertical-align: top; padding: 2px 12px 2px 0; }
    th { font-weight: normal; font-family: var(--vscode-editor-font-family); }
    code { font-family: var(--vscode-editor-font-family); white-space: pre-wrap; word-break: break-word; }
    .dim { color: var(--vscode-descriptionForeground); }
    .error { color: var(--vscode-errorForeground); }
    a { color: var(--vscode-textLink-foreground); text-decoration: none; }
    a:hover { text-decoration: underline; }
    a.origin { margin-left: 8px; font-size: 0.9em; opacity: 0.8; }
  </style>
</head>
<body>
${body}
<script nonce="${nonce}">
  const vscode = acquireVsCodeApi();
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (link) {
      event.preventDefault();
      if (link.dataset.origin) {
        vscode.postMessage({ type: "origin", name: link.dataset.origin, at: link.dataset.at ? Number(link.dataset.at) : undefined });
      } else {
        vscode.postMessage(link.dataset.caller ? { type: "openCaller", line: Number(link.dataset.caller) } : { type: "open", line: Number(link.dataset.line) });
      }
    }
  });
</script>
</body>
</html>`;
}
