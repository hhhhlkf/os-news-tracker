// frontend/src/discovery/flowState.ts
import type { DiscoveryRun, DiscoveryNodeTraceEntry } from "../types";

export type FlowNodeId =
  | "fetch_homepage" | "capture_network" | "explorer"
  | "validator" | "dsl_writer" | "auditor" | "save_method";

export interface FlowNodeMeta { id: FlowNodeId; label: string; kind: "det" | "agent"; }

export const FLOW_NODES: FlowNodeMeta[] = [
  { id: "fetch_homepage", label: "抓首页", kind: "det" },
  { id: "capture_network", label: "抓网络请求", kind: "det" },
  { id: "explorer", label: "探查", kind: "agent" },
  { id: "validator", label: "验证URL", kind: "agent" },
  { id: "dsl_writer", label: "写配方", kind: "agent" },
  { id: "auditor", label: "审计", kind: "agent" },
  { id: "save_method", label: "存库", kind: "det" },
];

export const FORWARD: FlowNodeId[] = [
  "fetch_homepage", "capture_network", "explorer",
  "validator", "dsl_writer", "auditor", "save_method",
];

export type NodeState = "pending" | "running" | "done" | "failed";

export function attemptCount(trace: DiscoveryNodeTraceEntry[]): number {
  const n = trace.filter((e) => e.step === "auditor").length;
  return Math.max(1, n);
}

function lastStep(trace: DiscoveryNodeTraceEntry[]): FlowNodeId | null {
  for (let i = trace.length - 1; i >= 0; i--) {
    if (FORWARD.includes(trace[i].step as FlowNodeId)) return trace[i].step as FlowNodeId;
  }
  return null;
}

function nextAfter(step: FlowNodeId | null): FlowNodeId | null {
  if (step === null) return FORWARD[0];
  const i = FORWARD.indexOf(step);
  if (i < 0 || i + 1 >= FORWARD.length) return null;
  return FORWARD[i + 1];
}

export function computeNodeStates(run: DiscoveryRun): Record<FlowNodeId, NodeState> {
  const states = Object.fromEntries(FLOW_NODES.map((n) => [n.id, "pending"])) as Record<FlowNodeId, NodeState>;
  const done = new Set(run.node_trace.map((e) => e.step));
  for (const id of FORWARD) if (done.has(id)) states[id] = "done";

  if (run.status === "completed") return states;

  const last = lastStep(run.node_trace);
  // Retry re-run: the last trace step is a rewrite target (dsl_writer/validator) AND it
  // already ran once before (appears >= 2 times in the trace) — i.e. auditor sent it back.
  const isRetryRetarget =
    (last === "dsl_writer" || last === "validator") &&
    run.node_trace.filter((e) => e.step === last).length >= 2;
  const running: FlowNodeId | null = isRetryRetarget ? last : nextAfter(last);

  if (run.status === "failed") {
    if (running) states[running] = "failed";
    return states;
  }
  if (running) states[running] = "running";
  return states;
}
