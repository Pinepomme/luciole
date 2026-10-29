/** Types for the Luciole graph format (see docs/graph-format.md and schema/graph.schema.json). */

export type NodeKind = "module" | "class" | "function" | "variable";
export type EdgeKind = "calls" | "reads" | "writes" | "flows_to" | "passes_arg" | "returns";
export type Role = "param" | "local" | "global" | "class_attr" | "instance_attr" | "return";

export interface Loc {
  line: number;
  col: number;
  end_line: number;
  end_col: number;
}

export interface GraphNode {
  id: string;
  kind: NodeKind;
  name: string;
  parent: string | null;
  file: string;
  loc: Loc;
  role?: Role;
  param_index?: number;
  is_async?: boolean;
  /** Functions: first line that runs on each call, where a breakpoint stops every call. */
  body_line?: number;
  decorators?: string[];
  bases?: string[];
}

export interface GraphEdge {
  kind: EdgeKind;
  source: string;
  target: string;
  sites?: { line: number; col: number }[];
  provenance?: "static" | "runtime";
  /** `calls` resolved by method name alone: likely, not certain. */
  by_name?: boolean;
  /** `calls` to a function passed as an argument, called later by what receives it. */
  callback?: boolean;
}

export interface Graph {
  format: "luciole-graph";
  format_version: number;
  root: string;
  generator?: { name: string; version: string };
  nodes: GraphNode[];
  edges: GraphEdge[];
}
