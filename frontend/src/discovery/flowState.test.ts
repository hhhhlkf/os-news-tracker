// frontend/src/discovery/flowState.test.ts
import { describe, it, expect } from "vitest";
import { computeNodeStates, attemptCount, FORWARD } from "./flowState";
import type { DiscoveryNodeTraceEntry, DiscoveryRun } from "../types";

function trace(steps: string[]): DiscoveryNodeTraceEntry[] {
  return steps.map((s) => ({ step: s, status: "done", ts: "t" }));
}

const baseRun = (over: Partial<DiscoveryRun>): DiscoveryRun => ({
  id: 1, site_url: "u", status: "running", resulting_method_id: null,
  llm_token_usage: 0, node_trace: [], retry_count: 0, current_step: null,
  started_at: null, ended_at: null, error_message: null, ...over,
});

describe("computeNodeStates", () => {
  it("空 trace + running → 第一个节点进行中", () => {
    const s = computeNodeStates(baseRun({ node_trace: [] }));
    expect(s["fetch_homepage"]).toBe("running");
    expect(s["explorer"]).toBe("pending");
  });
  it("fetch_homepage 完成 → capture_network 进行中", () => {
    const s = computeNodeStates(baseRun({ node_trace: trace(["fetch_homepage"]), current_step: "fetch_homepage" }));
    expect(s["fetch_homepage"]).toBe("done");
    expect(s["capture_network"]).toBe("running");
    expect(s["explorer"]).toBe("pending");
  });
  it("completed → 全部 done", () => {
    const s = computeNodeStates(baseRun({
      status: "completed",
      node_trace: trace(FORWARD), current_step: "save_method",
    }));
    expect(s["save_method"]).toBe("done");
    expect(s["auditor"]).toBe("done");
  });
  it("failed → 进行中节点标 failed", () => {
    const s = computeNodeStates(baseRun({
      status: "failed", node_trace: trace(["fetch_homepage", "capture_network", "explorer"]),
      current_step: "explorer", error_message: "boom",
    }));
    expect(s["validator"]).toBe("failed"); // 推断的进行中节点
    expect(s["explorer"]).toBe("done");
  });
  it("explorer summary.success=false 且最终失败 → explorer 标 failed", () => {
    const s = computeNodeStates(baseRun({
      status: "failed",
      node_trace: [
        { step: "fetch_homepage", status: "done", ts: "t" },
        { step: "capture_network", status: "done", ts: "t" },
        { step: "explorer", status: "done", ts: "t", summary: { success: false, source_type: "unknown" } },
      ],
      current_step: "explorer",
      error_message: "boom",
    }));
    expect(s["explorer"]).toBe("failed");
    expect(s["validator"]).toBe("pending");
  });
  it("重试：auditor 出现两次 → 写配方重新进行中", () => {
    const t = trace(["fetch_homepage", "capture_network", "explorer", "validator",
      "dsl_writer", "auditor", "dsl_writer"]);
    const s = computeNodeStates(baseRun({ node_trace: t, current_step: "dsl_writer" }));
    expect(s["auditor"]).toBe("done");
    expect(s["dsl_writer"]).toBe("running");
  });
  it("save_method 在 trace 里 → done，无进行中", () => {
    const s = computeNodeStates(baseRun({ status: "completed", node_trace: trace(FORWARD), current_step: "save_method" }));
    expect(Object.values(s).every((v) => v === "done")).toBe(true);
  });
});

describe("attemptCount", () => {
  it("无 auditor → 1 轮", () => {
    expect(attemptCount(trace(["fetch_homepage"]))).toBe(1);
  });
  it("auditor 出现 2 次 → 2 轮", () => {
    expect(attemptCount(trace(["explorer", "validator", "dsl_writer", "auditor", "dsl_writer", "auditor"]))).toBe(2);
  });
});
