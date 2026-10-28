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
  decorators?: string[];
  bases?: string[];
}

export interface GraphEdge {
  kind: EdgeKind;
  source: string;
  target: string;
  sites?: { line: number; col: number }[];
  provenance?: "static" | "runtime";
}

export interface Graph {
  format: "luciole-graph";
  format_version: number;
  root: string;
  generator?: { name: string; version: string };
  nodes: GraphNode[];
  edges: GraphEdge[];
}
