/** The Actions view: Luciole's commands, and the auto-start switch with its state. Pure logic (no VS Code). */

export interface ActionItem {
  label: string;
  /** A codicon name. */
  icon: string;
  command: string;
  description?: string;
  tooltip: string;
}

export function actionItems(autoStart: boolean): ActionItem[] {
  return [
    { label: "Show Graph for Current File", icon: "type-hierarchy", command: "luciole.showFileGraph", tooltip: "Graph the Python file in the editor, and follow the editor to other files" },
    { label: "Show Graph (Whole Folder)", icon: "folder-library", command: "luciole.showGraph", tooltip: "Graph the whole workspace folder" },
    { label: "Run Live", icon: "zap", command: "luciole.runLive", tooltip: "Run the current file (a script, a test file, or the tests of a module) and light up the brain" },
    { label: "Run Live…", icon: "run-all", command: "luciole.runLiveWith", tooltip: "Choose what to run: arguments, a module, tests, a run configuration" },
    autoStart
      ? {
          label: "Auto-Start",
          icon: "pass-filled",
          command: "luciole.toggleAutoStart",
          description: "on",
          tooltip: "Python programs started in new terminals light up the brain. Click to turn off.",
        }
      : {
          label: "Auto-Start",
          icon: "circle-large-outline",
          command: "luciole.toggleAutoStart",
          description: "off",
          tooltip: "Click to light up the brain with every Python program started in new terminals (pytest, scripts, servers).",
        },
  ];
}
