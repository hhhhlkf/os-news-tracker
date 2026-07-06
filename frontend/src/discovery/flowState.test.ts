// frontend/src/discovery/flowState.test.ts
import { describe, it, expect } from "vitest";
import { computeNodeStates, attemptCount, currentAttemptRound, FORWARD, computeEdgeStates } from "./flowState";
import type { DiscoveryNodeTraceEntry, DiscoveryRun } from "../types";

function trace(steps: string[]): DiscoveryNodeTraceEntry[] {
  return steps.map((s, index) => ({
    step: s,
    status: "done",
    ts: `2026-07-02T00:00:${String(index).padStart(2, "0")}Z`,
  }));
}

function step(
  name: string,
  summary?: Record<string, unknown>,
  ts: string = "2026-07-02T00:00:00Z",
): DiscoveryNodeTraceEntry {
  return { step: name, status: "done", ts, summary };
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
    expect(s["fetch_homepage"]).toBe("done");
    expect(s["capture_network"]).toBe("done");
    expect(s["explorer"]).toBe("done");
    expect(s["validator"]).toBe("done");
    expect(s["auditor"]).toBe("done");
    expect(s["dsl_writer"]).toBe("running");
  });
  it("auditor 不通过且任务仍在运行 → explorer 重新进行中", () => {
    const s = computeNodeStates(baseRun({
      node_trace: [
        step("fetch_homepage", undefined, "2026-07-02T00:00:00Z"),
        step("capture_network", undefined, "2026-07-02T00:00:01Z"),
        step("explorer", undefined, "2026-07-02T00:00:02Z"),
        step("validator", undefined, "2026-07-02T00:00:03Z"),
        step("dsl_writer", undefined, "2026-07-02T00:00:04Z"),
        step("auditor", { passed: false, attempt: 1 }, "2026-07-02T00:00:05Z"),
      ],
      current_step: "auditor",
    }));
    expect(s["auditor"]).toBe("pending");
    expect(s["explorer"]).toBe("running");
    expect(s["save_method"]).toBe("pending");
  });
  it("auditor 不通过后的 1 秒内保持 warning，再回到灰色", () => {
    const run = baseRun({
      node_trace: [
        step("fetch_homepage", undefined, "2026-07-02T00:00:00Z"),
        step("capture_network", undefined, "2026-07-02T00:00:01Z"),
        step("explorer", undefined, "2026-07-02T00:00:02Z"),
        step("validator", undefined, "2026-07-02T00:00:03Z"),
        step("dsl_writer", undefined, "2026-07-02T00:00:04Z"),
        step("auditor", { passed: false, attempt: 1 }, "2026-07-02T00:00:05Z"),
      ],
      current_step: "auditor",
    });

    const warning = computeNodeStates(run, Date.parse("2026-07-02T00:00:05.500Z"));
    const cooled = computeNodeStates(run, Date.parse("2026-07-02T00:00:06.200Z"));
    expect(warning["auditor"]).toBe("warning");
    expect(warning["explorer"]).toBe("running");
    expect(warning["validator"]).toBe("pending");
    expect(warning["dsl_writer"]).toBe("pending");
    expect(cooled["auditor"]).toBe("pending");
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

describe("currentAttemptRound", () => {
  it("首轮运行时显示第 1 轮", () => {
    const round = currentAttemptRound(baseRun({
      node_trace: [step("fetch_homepage")],
      current_step: "fetch_homepage",
    }));
    expect(round).toBe(1);
  });

  it("第一次 auditor 不通过后，回到 explorer 时显示第 2 轮", () => {
    const round = currentAttemptRound(baseRun({
      node_trace: [
        step("fetch_homepage"),
        step("capture_network"),
        step("explorer"),
        step("validator"),
        step("dsl_writer"),
        step("auditor", { passed: false, attempt: 1 }),
      ],
      current_step: "auditor",
    }));
    expect(round).toBe(2);
  });

  it("第一次重试已经推进到 validator 时，仍显示第 2 轮", () => {
    const round = currentAttemptRound(baseRun({
      node_trace: [
        step("fetch_homepage"),
        step("capture_network"),
        step("explorer"),
        step("validator"),
        step("dsl_writer"),
        step("auditor", { passed: false, attempt: 1 }),
        step("explorer"),
        step("validator"),
      ],
      current_step: "validator",
    }));
    expect(round).toBe(2);
  });
});

describe("computeEdgeStates", () => {
  it("初始运行时所有边保持 pending", () => {
    const s = computeEdgeStates(baseRun({ node_trace: [] }));
    expect(s["fetch_homepage->capture_network"]).toBe("pending");
    expect(s["capture_network->explorer"]).toBe("pending");
    expect(s["auditor->explorer"]).toBe("pending");
  });

  it("fetch_homepage 完成后指向 capture_network 的边变为 active", () => {
    const s = computeEdgeStates(baseRun({
      node_trace: trace(["fetch_homepage"]),
      current_step: "fetch_homepage",
    }));
    expect(s["fetch_homepage->capture_network"]).toBe("active");
    expect(s["capture_network->explorer"]).toBe("pending");
  });

  it("中间运行态已走过主路径边变 done，当前目标边变 active", () => {
    const s = computeEdgeStates(baseRun({
      node_trace: trace(["fetch_homepage", "capture_network"]),
      current_step: "capture_network",
    }));
    expect(s["fetch_homepage->capture_network"]).toBe("done");
    expect(s["capture_network->explorer"]).toBe("active");
    expect(s["explorer->validator"]).toBe("pending");
  });

  it("重试回流到 explorer 时 auditor 回环边变 active，历史主路径保持 done", () => {
    const s = computeEdgeStates(baseRun({
      node_trace: [
        step("fetch_homepage", undefined, "2026-07-02T00:00:00Z"),
        step("capture_network", undefined, "2026-07-02T00:00:01Z"),
        step("explorer", undefined, "2026-07-02T00:00:02Z"),
        step("validator", undefined, "2026-07-02T00:00:03Z"),
        step("dsl_writer", undefined, "2026-07-02T00:00:04Z"),
        step("auditor", { passed: false, attempt: 1 }, "2026-07-02T00:00:05Z"),
      ],
      current_step: "auditor",
    }));
    expect(s["fetch_homepage->capture_network"]).toBe("done");
    expect(s["capture_network->explorer"]).toBe("done");
    expect(s["auditor->explorer"]).toBe("retrying");
    expect(s["explorer->validator"]).toBe("pending");
    expect(s["validator->dsl_writer"]).toBe("pending");
  });

  it("局部重写时 auditor -> dsl_writer 回环边变 retrying", () => {
    const s = computeEdgeStates(baseRun({
      node_trace: [
        step("fetch_homepage", undefined, "2026-07-02T00:00:00Z"),
        step("capture_network", undefined, "2026-07-02T00:00:01Z"),
        step("explorer", undefined, "2026-07-02T00:00:02Z"),
        step("validator", undefined, "2026-07-02T00:00:03Z"),
        step("dsl_writer", undefined, "2026-07-02T00:00:04Z"),
        step("auditor", { passed: false, decision: "rewrite", dsl_cycle_attempt: 1 }, "2026-07-02T00:00:05Z"),
      ],
      current_step: "auditor",
    }));
    expect(s["auditor->dsl_writer"]).toBe("retrying");
    expect(s["auditor->explorer"]).toBe("pending");
  });
});

describe("rewrite retry visuals", () => {
  it("auditor 判定 rewrite 后，上游步骤保持 done，写配方进入 running", () => {
    const s = computeNodeStates(baseRun({
      node_trace: [
        step("fetch_homepage", undefined, "2026-07-02T00:00:00Z"),
        step("capture_network", undefined, "2026-07-02T00:00:01Z"),
        step("explorer", undefined, "2026-07-02T00:00:02Z"),
        step("validator", undefined, "2026-07-02T00:00:03Z"),
        step("dsl_writer", undefined, "2026-07-02T00:00:04Z"),
        step("auditor", { passed: false, decision: "rewrite", dsl_cycle_attempt: 1 }, "2026-07-02T00:00:05Z"),
      ],
      current_step: "auditor",
    }), Date.parse("2026-07-02T00:00:05.500Z"));

    expect(s["fetch_homepage"]).toBe("done");
    expect(s["capture_network"]).toBe("done");
    expect(s["explorer"]).toBe("done");
    expect(s["validator"]).toBe("done");
    expect(s["dsl_writer"]).toBe("running");
    expect(s["auditor"]).toBe("warning");
  });
});
