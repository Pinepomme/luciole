/**
 * "Where did this value come from?" in VS Code: the answer of an origin query (`origin` in
 * src/luciole/browse.py, the walk in src/luciole/origins.py, docs/origins.md) as items of the
 * Origin view, the path it lights on the brain, and what the editor asks about (the name under
 * the cursor). Pure logic (no VS Code); the view and its commands are in originview.ts.
 */

import { argsText, outcomeText, shortNode, Snapshot, TraceCall } from "./calls";

/** One step of an origin answer: see `origin()` in src/luciole/origins.py. */
export interface OriginStep {
  kind: "write" | "argument" | "returned_by" | "attribute_written_by" | "global_written_by" | "literal" | "needs_level_2" | "unknown";
  /** As the code names it (`due`, `Cart.subtotal`), and its node id. */
  variable: string | null;
  var: string | null;
  /** The call it is about. */
  frame: number | null;
  node: string | null;
  /** The line that made the value, and its code. */
  line: number | null;
  code: string | null;
  /** A snapshot's repr, null when not recorded. */
  value: string | null;
  note: string | null;
  children: OriginStep[];
  caller?: { frame: number; node: string | null; line: number | null; code: string | null };
  /** The value an augmented assignment (`x += y`) started from. */
  previous?: boolean;
  time_ns?: number;
  /** needs_level_2: the function to record at level 2. */
  rerun?: string;
  candidates?: { line: number; code: string | null; ran?: boolean; same_value?: boolean }[];
  changed_in_place?: { frame: number; node: string; line: number | null; lines?: number[]; code: string | null; calls: number };
  locals?: { name: string; value: string | null; line: number }[];
  seen?: boolean;
  cut?: "depth" | "steps";
  more?: number;
  not_followed?: string[];
}

export interface OriginAnswer {
  frame: number;
  node: string;
  variable: string;
  var: string;
  time_ns: number;
  level: number;
  scope: string[] | null;
  steps: OriginStep;
  /** The project root the node ids are of, and each step's function's file, relative to it. */
  root: string;
  files: Record<string, string>;
  /** The answer as `luciole trace FILE origin` prints it. */
  text: string;
}

/** A question: one variable of one recorded call. */
export interface OriginTarget {
  /** The trace (one process of a run) and the project folder of its run. */
  file: string;
  root: string;
  frame: number;
  node: string;
  /** A local or parameter, `return`, `self.x`, a global. */
  variable: string;
  /** The moment (ns since the run started); default: the call's end. */
  at?: number;
}

/** What the brain lights: the functions of the steps, and the value's moves between them. */
export interface OriginPath {
  /** Where the question was asked (its function), and every function of the answer. */
  start: string;
  nodes: string[];
  /** From the function that made a value to the one that took it; `depth` from the question (1: into the start). */
  links: { from: string; to: string; depth: number }[];
  /** `due in checkout #4`, for the status line. */
  label: string;
}

export interface OriginItem {
  key: string;
  label: string;
  description?: string;
  tooltip?: string;
  icon?: string;
  children: OriginItem[];
  /** step: one step of the chain; detail: what render_origin prints under it; rerun, continue: actions. */
  kind: "step" | "detail" | "rerun" | "continue";
  /** Clicking it opens this function at this line (else where it is defined), and pulses it on the brain. */
  at?: { node: string; line: number | null };
  /** rerun: the function to record at level 2. continue: the question to ask from there. */
  scope?: string;
  question?: { frame: number; variable: string; at?: number };
}

const VALUE_WIDTH = 80;

function clip(text: string | null | undefined, width = VALUE_WIDTH): string {
  if (text === null || text === undefined) {
    return "?";
  }
  const line = text.replace(/\s+/g, " ");
  return line.length > width ? `${line.slice(0, width - 1)}…` : line;
}

/** `Cart.add` for `shop:Cart.add`, `?` when unknown. */
function short(node: string | null | undefined): string {
  return node ? shortNode(node) : "?";
}

/** The step's text, as render_origin writes it: a label (the value) and a description (where). */
export function stepText(step: OriginStep): { label: string; description: string } {
  const where = `${short(step.node)} #${step.frame}`;
  const at = step.line ? `line ${step.line}${step.code ? `: ${step.code}` : ""}` : "";
  const value = step.value !== null ? ` = ${clip(step.value)}` : "";
  const variable = step.variable ?? "";
  let label: string;
  let description: string;
  switch (step.kind) {
    case "write":
      [label, description] = [`${variable}${value}`, [where, at].filter(Boolean).join(", ")];
      break;
    case "argument": {
      const caller = step.caller;
      const passed = caller ? `, passed by ${short(caller.node)} #${caller.frame}, line ${caller.line}${caller.code ? `: ${caller.code}` : ""}` : "";
      [label, description] = [`${variable}${value}`, `argument of ${where}${passed}`];
      break;
    }
    case "returned_by":
      [label, description] = [`returned by ${where}${step.value !== null ? `: ${clip(step.value)}` : ""}`, at];
      break;
    case "attribute_written_by":
    case "global_written_by":
      [label, description] = [`${variable}${value} set by ${where}`, at];
      break;
    case "needs_level_2": {
      const lines = (step.candidates ?? []).map((candidate) => candidate.line);
      [label, description] = [`${variable}${value}`, `${where}: assigned on line${lines.length > 1 ? "s" : ""} ${lines.join(", ")}`];
      break;
    }
    case "literal":
      [label, description] = ["no variable", step.line ? `${where}, ${at}` : ""];
      break;
    default:
      [label, description] = [`?${variable ? ` ${variable}` : ""}`, [where, at].filter(Boolean).join(", ")];
  }
  if (step.previous) {
    label = `before that: ${label}`;
  }
  if (step.note) {
    description = description ? `${description}   (${step.note})` : `(${step.note})`;
  }
  return { label, description };
}

const ICONS: Record<OriginStep["kind"], string> = {
  write: "symbol-variable",
  argument: "symbol-parameter",
  returned_by: "debug-step-out",
  attribute_written_by: "symbol-field",
  global_written_by: "symbol-constant",
  literal: "symbol-string",
  needs_level_2: "question",
  unknown: "question",
};

/** The answer as the Origin view's tree: the question's step, its sources below it, and so on. */
export function originItems(answer: OriginAnswer): OriginItem[] {
  return [stepItem(answer.steps, "0")];
}

function stepItem(step: OriginStep, key: string): OriginItem {
  const { label, description } = stepText(step);
  const details: OriginItem[] = [];
  const detail = (suffix: string, text: string, extra: Partial<OriginItem> = {}) =>
    details.push({ key: `${key}.${suffix}`, label: text, kind: "detail", children: [], icon: "blank", ...extra });
  (step.candidates ?? []).forEach((candidate, i) => {
    const ran = candidate.ran ? "a call on it ran" : candidate.same_value ? "returns exactly this value" : "";
    detail(`c${i}`, `line ${candidate.line}: ${candidate.code ?? ""}`, { description: ran, icon: "arrow-small-right", at: step.node ? { node: step.node, line: candidate.line } : undefined });
  });
  const changed = step.changed_in_place;
  if (changed) {
    const where = changed.line ? `, line ${changed.line}: ${changed.code ?? ""}` : changed.lines?.length ? `, lines ${changed.lines.join(", ")}` : "";
    const more = changed.calls > 1 ? `; ${changed.calls} such calls since it was set` : "";
    detail("changed", `then changed in place by ${short(changed.node)} #${changed.frame}${where}`, {
      description: `its contents are not recorded${more}`,
      icon: "edit",
      at: { node: changed.node, line: changed.line ?? changed.lines?.[0] ?? null },
    });
  }
  if (step.locals?.length) {
    detail("locals", `its last writes: ${step.locals.map((found) => `${found.name} = ${clip(found.value, 60)} (line ${found.line})`).join(", ")}`);
  }
  if (step.kind === "needs_level_2" && step.rerun) {
    details.push({
      key: `${key}.rerun`,
      label: `Record ${shortNode(step.rerun)} line by line and run again`,
      description: "level 2",
      tooltip: `Runs the same command again, recording every assignment in ${step.rerun} (level 2), so that asking again says which line made this value.\n\nFrom a terminal: luciole run --scope ${step.rerun} --trace DIR …`,
      kind: "rerun",
      icon: "debug-rerun",
      scope: step.rerun,
      children: [],
    });
  }
  if (step.seen) {
    detail("seen", "(followed above)");
  }
  if (step.cut && step.frame !== null) {
    const ask = step.kind === "returned_by" ? "return" : step.kind.endsWith("_written_by") ? step.var : step.variable;
    if (ask) {
      details.push({
        key: `${key}.continue`,
        label: `Not followed further (${step.cut === "depth" ? "depth" : "step"} limit): go on from here`,
        kind: "continue",
        icon: "debug-continue",
        question: { frame: step.frame, variable: ask, ...(step.kind === "write" && step.time_ns !== undefined ? { at: step.time_ns } : {}) },
        children: [],
      });
    }
  }
  const children = step.children.map((child, i) => stepItem(child, `${key}.${i}`));
  if (step.more) {
    const names = (step.not_followed ?? []).slice(0, 12).join(", ");
    children.push({ key: `${key}.more`, label: `… ${step.more} more sources not followed (branch limit)`, description: names, kind: "detail", icon: "ellipsis", children: [] });
  }
  const tooltip = [
    `${label}${description ? `\n${description}` : ""}`,
    step.value !== null && step.value.length > VALUE_WIDTH ? `\n${step.value}` : "",
    step.node ? `\n${step.node}${step.frame !== null ? ` #${step.frame}` : ""}` : "",
  ].join("");
  return {
    key,
    label,
    description,
    tooltip,
    icon: ICONS[step.kind] ?? "question",
    kind: "step",
    at: step.node ? { node: step.node, line: step.line } : undefined,
    children: [...details, ...children],
  };
}

/**
 * The path to light on the brain: each step's function, and a move from the function that made
 * a value to the one that took it (a parameter: from its caller; a source of a step: into that
 * step's function). Moves inside one function draw nothing.
 */
export function originPath(answer: OriginAnswer): OriginPath {
  const nodes: string[] = [];
  const links = new Map<string, { from: string; to: string; depth: number }>();
  const addNode = (node: string | null | undefined) => {
    if (node && !nodes.includes(node)) {
      nodes.push(node);
    }
  };
  const addLink = (from: string | null | undefined, to: string | null | undefined, depth: number) => {
    if (from && to && from !== to && !links.has(`${from}>${to}`)) {
      links.set(`${from}>${to}`, { from, to, depth });
    }
  };
  const visit = (step: OriginStep, consumer: string | null, depth: number) => {
    addNode(step.node);
    addLink(step.node, consumer, depth);
    if (step.caller?.node) {
      addNode(step.caller.node);
      addLink(step.caller.node, step.node, depth + 1);
    }
    if (step.changed_in_place) {
      addNode(step.changed_in_place.node);
      addLink(step.changed_in_place.node, step.node, depth + 1);
    }
    step.children.forEach((child) => visit(child, step.node, depth + 1));
  };
  visit(answer.steps, null, 0);
  addNode(answer.node);
  return { start: answer.node, nodes, links: [...links.values()], label: `${answer.variable} in ${shortNode(answer.node)} #${answer.frame}` };
}

/** The question, for the view's title: `where n in leaf #4 came from (level 1)`. */
export function originTitle(answer: OriginAnswer): string {
  return `${answer.variable} in ${shortNode(answer.node)} #${answer.frame}, level ${answer.level}`;
}

const KEYWORDS = new Set(
  "False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise try while with yield".split(" "),
);

/**
 * The variable the editor asks about at column `col` of `line`: a name (`total`), an attribute of
 * `self` or `cls` (`self.balance`), or `return` (what the call returned). Undefined elsewhere:
 * another object's attribute, a keyword, nothing.
 */
export function identifierAt(line: string, col: number): string | undefined {
  for (const match of line.matchAll(/[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*/g)) {
    const start = match.index ?? 0;
    if (col < start || col > start + match[0].length) {
      continue;
    }
    if (start > 0 && /[\w.]/.test(line[start - 1])) {
      return undefined; // part of a number, or of something before a dot that is not a name
    }
    const parts = match[0].split(".");
    let end = start;
    const index = parts.findIndex((part) => (end += part.length + 1) > col);
    const at = index < 0 ? parts.length - 1 : index;
    if ((parts[0] === "self" || parts[0] === "cls") && parts.length > 1 && at <= 1) {
      return `${parts[0]}.${parts[1]}`;
    }
    if (at !== 0 || (KEYWORDS.has(parts[0]) && parts[0] !== "return") || parts[0] === "self" || parts[0] === "cls") {
      return undefined;
    }
    return parts[0];
  }
  return undefined;
}

/** A call's values to ask about: its arguments (from when it started), then what it returned. */
export function callValues(call: TraceCall): { variable: string; value: Snapshot | null | undefined; at?: number }[] {
  const values: { variable: string; value: Snapshot | null | undefined; at?: number }[] = Object.entries(call.args ?? {}).map(([variable, value]) => ({ variable, value, at: call.start_ns }));
  if (call.end === "return" && call.result !== undefined) {
    values.push({ variable: "return", value: call.result });
  }
  for (const [variable, value] of Object.entries(call.locals ?? {})) {
    if (!values.some((known) => known.variable === variable)) {
      values.push({ variable, value });
    }
  }
  return values;
}

/** A call in a quick pick: `leaf(n=100) → 200`, and where it was. */
export function callChoice(call: TraceCall, when: string): { label: string; description: string } {
  return {
    label: `${shortNode(call.node)}${argsText(call.args, 100)} ${outcomeText(call)}`.trim(),
    description: `#${call.frame}${call.caller?.node ? ` · called by ${shortNode(call.caller.node)} at line ${call.caller.line}` : ""} · ${when}`,
  };
}

/**
 * The command a run was started with, as a RunConfiguration's target: a script, or `-m module`
 * (a tool run by name, `pytest -x`, as `-m pytest -x`).
 */
export function runTarget(command: string[]): { program?: string; module?: string; args: string[] } | undefined {
  const name = command[0]?.split(/[\\/]/).pop() ?? "";
  if (!/^python[\d.]*(\.exe)?$/i.test(name)) {
    return name && !name.endsWith(".py") ? { module: name.replace(/\.exe$/i, ""), args: command.slice(1) } : command[0] ? { program: command[0], args: command.slice(1) } : undefined;
  }
  const target = command.slice(1);
  if (target[0] === "-m" && target[1]) {
    return { module: target[1], args: target.slice(2) };
  }
  return target[0] && !target[0].startsWith("-") ? { program: target[0], args: target.slice(1) } : undefined;
}
