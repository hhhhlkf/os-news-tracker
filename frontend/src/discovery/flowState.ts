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

const RETRY_WARNING_MS = 1000;

export type NodeState = "pending" | "running" | "done" | "failed" | "warning";
export type FlowEdgeId =
  | "fetch_homepage->capture_network"
  | "capture_network->explorer"
  | "explorer->validator"
  | "validator->dsl_writer"
  | "dsl_writer->auditor"
  | "auditor->save_method"
  | "auditor->explorer";

export type EdgeState = "pending" | "active" | "done" | "retrying";

export interface FlowEdgeMeta {
  id: FlowEdgeId;
  source: FlowNodeId;
  target: FlowNodeId;
  kind: "forward" | "retry";
}

export const FLOW_EDGES: FlowEdgeMeta[] = [
  { id: "fetch_homepage->capture_network", source: "fetch_homepage", target: "capture_network", kind: "forward" },
  { id: "capture_network->explorer", source: "capture_network", target: "explorer", kind: "forward" },
  { id: "explorer->validator", source: "explorer", target: "validator", kind: "forward" },
  { id: "validator->dsl_writer", source: "validator", target: "dsl_writer", kind: "forward" },
  { id: "dsl_writer->auditor", source: "dsl_writer", target: "auditor", kind: "forward" },
  { id: "auditor->save_method", source: "auditor", target: "save_method", kind: "forward" },
  { id: "auditor->explorer", source: "auditor", target: "explorer", kind: "retry" },
];

export function attemptCount(trace: DiscoveryNodeTraceEntry[]): number {
  const n = trace.filter((e) => e.step === "auditor").length;
  return Math.max(1, n);
}

export function currentAttemptRound(run: DiscoveryRun): number {
  const latestAuditor = latestEntryForStep(run.node_trace, "auditor");
  const failedAttempts = typeof latestAuditor?.summary?.attempt === "number"
    ? latestAuditor.summary.attempt
    : attemptCount(run.node_trace);

  if (latestAuditor?.summary?.passed !== false) {
    return Math.max(1, failedAttempts);
  }

  const latestAuditorIndex = run.node_trace.lastIndexOf(latestAuditor);
  const hasRestartedAfterFailure = latestAuditorIndex >= 0 && latestAuditorIndex < run.node_trace.length - 1;
  const running = inferredRunningStep(run);

  if (hasRestartedAfterFailure || running === "explorer" || running === "validator" || running === "dsl_writer" || running === "auditor") {
    return failedAttempts + 1;
  }

  return Math.max(1, failedAttempts);
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

function latestEntryForStep(
  trace: DiscoveryNodeTraceEntry[],
  step: FlowNodeId,
): DiscoveryNodeTraceEntry | null {
  for (let i = trace.length - 1; i >= 0; i--) {
    if (trace[i].step === step) return trace[i];
  }
  return null;
}

function latestFailedAuditor(
  trace: DiscoveryNodeTraceEntry[],
): { entry: DiscoveryNodeTraceEntry; index: number; timestampMs: number } | null {
  for (let i = trace.length - 1; i >= 0; i--) {
    const entry = trace[i];
    if (entry.step !== "auditor" || entry.summary?.passed !== false) continue;
    return {
      entry,
      index: i,
      timestampMs: Number.isFinite(Date.parse(entry.ts)) ? Date.parse(entry.ts) : 0,
    };
  }
  return null;
}

function isRetryActive(run: DiscoveryRun): boolean {
  return run.status === "running" && latestFailedAuditor(run.node_trace) !== null;
}

function retrySegment(trace: DiscoveryNodeTraceEntry[]): DiscoveryNodeTraceEntry[] {
  const failed = latestFailedAuditor(trace);
  if (!failed) return [];
  return trace.slice(failed.index + 1);
}

function inferredRunningStep(run: DiscoveryRun): FlowNodeId | null {
  if (run.status === "completed" || run.status === "cancelled") return null;

  const last = lastStep(run.node_trace);
  const latestAuditor = latestEntryForStep(run.node_trace, "auditor");
  const latestAuditorPassed = latestAuditor?.summary?.passed;

  if (last === "auditor") {
    if (latestAuditorPassed === false) return "explorer";
    if (latestAuditorPassed === true) return "save_method";
  }

  const isRetryRetarget =
    (last === "dsl_writer" || last === "validator") &&
    run.node_trace.filter((e) => e.step === last).length >= 2;

  return isRetryRetarget ? last : nextAfter(last);
}

function hasTraversedEdge(
  trace: DiscoveryNodeTraceEntry[],
  source: FlowNodeId,
  target: FlowNodeId,
): boolean {
  for (let i = 0; i < trace.length; i++) {
    if (trace[i].step !== source) continue;
    for (let j = i + 1; j < trace.length; j++) {
      if (trace[j].step === target) return true;
    }
  }
  return false;
}

function businessFailedStep(trace: DiscoveryNodeTraceEntry[]): FlowNodeId | null {
  for (let i = trace.length - 1; i >= 0; i--) {
    const entry = trace[i];
    if (entry.step === "explorer" && entry.summary?.success === false) {
      return "explorer";
    }
  }
  return null;
}

export function computeNodeStates(
  run: DiscoveryRun,
  nowMs: number = Date.now(),
): Record<FlowNodeId, NodeState> {
  const states = Object.fromEntries(FLOW_NODES.map((n) => [n.id, "pending"])) as Record<FlowNodeId, NodeState>;
  const done = new Set(run.node_trace.map((e) => e.step));
  for (const id of FORWARD) if (done.has(id)) states[id] = "done";

  if (run.status === "completed" || run.status === "cancelled") return states;

  if (isRetryActive(run)) {
    const failed = latestFailedAuditor(run.node_trace);
    const retrySteps = new Set(retrySegment(run.node_trace).map((e) => e.step));
    states["fetch_homepage"] = done.has("fetch_homepage") ? "done" : "pending";
    states["capture_network"] = done.has("capture_network") ? "done" : "pending";
    states["explorer"] = retrySteps.has("explorer") ? "done" : "pending";
    states["validator"] = retrySteps.has("validator") ? "done" : "pending";
    states["dsl_writer"] = retrySteps.has("dsl_writer") ? "done" : "pending";
    states["auditor"] = "pending";
    states["save_method"] = retrySteps.has("save_method") ? "done" : "pending";

    const running = inferredRunningStep(run);
    if (running) states[running] = "running";
    if (failed && failed.timestampMs > 0 && nowMs - failed.timestampMs < RETRY_WARNING_MS) {
      states["auditor"] = "warning";
    }
    return states;
  }

  const running = inferredRunningStep(run);

  if (run.status === "failed") {
    const failedStep = businessFailedStep(run.node_trace);
    if (failedStep) {
      states[failedStep] = "failed";
      return states;
    }
    if (running) states[running] = "failed";
    return states;
  }
  if (running) states[running] = "running";
  return states;
}

export function computeEdgeStates(run: DiscoveryRun): Record<FlowEdgeId, EdgeState> {
  const states = Object.fromEntries(
    FLOW_EDGES.map((edge) => [edge.id, "pending"]),
  ) as Record<FlowEdgeId, EdgeState>;

  if (isRetryActive(run)) {
    const fullTrace = run.node_trace;
    const retryTrace = retrySegment(run.node_trace);
    states["fetch_homepage->capture_network"] = hasTraversedEdge(fullTrace, "fetch_homepage", "capture_network") ? "done" : "pending";
    states["capture_network->explorer"] = hasTraversedEdge(fullTrace, "capture_network", "explorer") ? "done" : "pending";
    states["explorer->validator"] = hasTraversedEdge(retryTrace, "explorer", "validator") ? "done" : "pending";
    states["validator->dsl_writer"] = hasTraversedEdge(retryTrace, "validator", "dsl_writer") ? "done" : "pending";
    states["dsl_writer->auditor"] = hasTraversedEdge(retryTrace, "dsl_writer", "auditor") ? "done" : "pending";
    states["auditor->save_method"] = hasTraversedEdge(retryTrace, "auditor", "save_method") ? "done" : "pending";
    states["auditor->explorer"] = "retrying";

    const running = inferredRunningStep(run);
    if (running && running !== "explorer") {
      const activeEdge = FLOW_EDGES.find((edge) => edge.kind === "forward" && edge.target === running);
      if (activeEdge) {
        states[activeEdge.id] = "active";
      }
    }
    return states;
  }

  for (const edge of FLOW_EDGES) {
    if (edge.kind !== "forward") continue;
    if (hasTraversedEdge(run.node_trace, edge.source, edge.target)) {
      states[edge.id] = "done";
    }
  }

  if (run.status !== "running") return states;

  const running = inferredRunningStep(run);
  if (!running) return states;

  const latestAuditor = latestEntryForStep(run.node_trace, "auditor");
  const latestAuditorPassed = latestAuditor?.summary?.passed;
  if (running === "explorer" && latestAuditorPassed === false) {
    states["auditor->explorer"] = "retrying";
    return states;
  }

  const activeEdge = FLOW_EDGES.find((edge) => edge.kind === "forward" && edge.target === running);
  if (activeEdge) {
    states[activeEdge.id] = "active";
  }
  return states;
}
