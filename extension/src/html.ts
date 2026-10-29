/** HTML of the graph webview. Kept free of `vscode` imports so the dev harness can reuse it. */

export interface PageOptions {
  /** Origin allowed to serve the script and stylesheet (`webview.cspSource` in VS Code). */
  cspSource: string;
  nonce: string;
  scriptUri: string;
  styleUri: string;
}

export function webviewHtml({ cspSource, nonce, scriptUri, styleUri }: PageOptions): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}'; img-src ${cspSource} data: blob:;">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="${styleUri}">
  <title>Luciole</title>
</head>
<body>
  <div id="toolbar">
    <button id="live" class="primary" title="Run the last run again (or this file), lighting up functions as they run">Run live</button>
    <button id="runWith" title="Choose what to run: a file with arguments, a configured run, a module (pytest…)">Run…</button>
    <button id="stop" class="stop" hidden title="Stop the program: its debug session, or Ctrl+C in its terminal (press again to kill it)">Stop</button>
    <button id="refresh" title="Run the analyzer again">Refresh</button>
    <button id="fit" title="Fit the brain in the window">Fit</button>
    <span id="status"></span>
  </div>
  <div id="notice" hidden>
    <span>Running without the Python debugger: the brain lights up, but there are no breakpoints or stepping.</span>
    <button id="installDebugger" class="primary" title="Install the Python Debugger extension (ms-python.debugpy) from the Marketplace">Install Python Debugger</button>
  </div>
  <div id="message" hidden></div>
  <div id="brain"></div>
  <div id="path" title="What is running now, outermost first"></div>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
}
