import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AgentRunControl } from "./AgentRunControl";
import { formatAgentRunStats, formatAgentStageLabel } from "./AgentSourceRunCard";
import type { AgentRunRecord, AgentSource } from "../types";

const source: AgentSource = {
  id: 1,
  name: "Kernel Watch",
  url: "https://example.com",
  enabled: true,
  config: {
    focus_areas: ["kernel", "scheduler"],
    topic_groups: ["项目动态"],
    crawl_depth: 1,
    max_urls_per_run: 20,
    quality_threshold: 4,
    crawl_workers: 5,
    quality_workers: 3,
    summary_workers: 3,
  },
};

function makeRun(partial: Partial<AgentRunRecord>): AgentRunRecord {
  return {
    id: 11,
    status: "running",
    current_stage: "planning",
    stage_message: null,
    plan_urls_count: 3,
    fetched_count: 0,
    quality_passed: 0,
    items_created: 0,
    started_at: null,
    completed_at: null,
    error_message: null,
    ...partial,
  };
}

describe("AgentSourceRunCard helpers", () => {
  it("formats stage labels for UI", () => {
    expect(formatAgentStageLabel("planning")).toBe("规划 URL");
    expect(formatAgentStageLabel("completed")).toBe("已完成");
    expect(formatAgentStageLabel(null)).toBe("尚未运行");
  });

  it("formats progress summaries for running stages", () => {
    expect(formatAgentRunStats(makeRun({ current_stage: "crawling", fetched_count: 2 }))).toContain("已抓取 2 / 3");
    expect(formatAgentRunStats(makeRun({ current_stage: "quality", fetched_count: 4, quality_passed: 2 }))).toContain("质量通过 2 / 4");
  });
});

describe("AgentRunControl", () => {
  it("renders running state, metrics and disables current source button", () => {
    const html = renderToStaticMarkup(
      createElement(AgentRunControl, {
        sources: [source],
        runsBySourceId: {
          1: [makeRun({ current_stage: "crawling", fetched_count: 2, stage_message: "并行抓取 3 个 URL" })],
        },
        onTrigger: () => {},
      }),
    );

    expect(html).toContain("Kernel Watch");
    expect(html).toContain("并行抓取");
    expect(html).toContain("运行中");
    expect(html).toContain("并行抓取 3 个 URL");
  });

  it("renders trigger errors and terminal states", () => {
    const html = renderToStaticMarkup(
      createElement(AgentRunControl, {
        sources: [source],
        runsBySourceId: {
          1: [makeRun({ status: "failed", current_stage: "failed", error_message: "boom" })],
        },
        triggerErrors: { 1: "重复触发" },
        onTrigger: () => {},
      }),
    );

    expect(html).toContain("失败");
    expect(html).toContain("重复触发");
  });
});
