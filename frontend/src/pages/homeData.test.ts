import { describe, expect, it } from "vitest";
import { agentCandidateRunRefreshKeys, buildAgentWarmupRun, buildDemoFacets, filterDemoItems, isAgentSourceRunning, isAgentWarmupResolved, isManualNewsRunActive, resolveHomeDataMode } from "./homeData";
import { demoItems } from "../demoData";

describe("resolveHomeDataMode", () => {
  it("falls back to demo mode when either live query fails", () => {
    expect(resolveHomeDataMode({ itemsFailed: true, facetsFailed: false })).toBe("demo");
    expect(resolveHomeDataMode({ itemsFailed: false, facetsFailed: true })).toBe("demo");
  });

  it("keeps live mode when both queries succeed", () => {
    expect(resolveHomeDataMode({ itemsFailed: false, facetsFailed: false })).toBe("live");
  });
});

describe("isManualNewsRunActive", () => {
  it("treats running states as active", () => {
    expect(isManualNewsRunActive("collecting")).toBe(true);
    expect(isManualNewsRunActive("processing")).toBe(true);
    expect(isManualNewsRunActive("stopping")).toBe(true);
  });

  it("treats terminal and empty states as inactive", () => {
    expect(isManualNewsRunActive("completed")).toBe(false);
    expect(isManualNewsRunActive("failed")).toBe(false);
    expect(isManualNewsRunActive("stopped")).toBe(false);
    expect(isManualNewsRunActive(null)).toBe(false);
  });
});

describe("isAgentSourceRunning", () => {
  it("treats in-progress agent stages as active", () => {
    expect(isAgentSourceRunning({
      id: 1,
      status: "running",
      current_stage: "planning",
      stage_message: null,
      plan_urls_count: 0,
      fetched_count: 0,
      quality_passed: 0,
      items_created: 0,
      started_at: null,
      completed_at: null,
      error_message: null,
    })).toBe(true);
    expect(isAgentSourceRunning({
      id: 2,
      status: "completed",
      current_stage: "quality",
      stage_message: null,
      plan_urls_count: 3,
      fetched_count: 2,
      quality_passed: 1,
      items_created: 0,
      started_at: null,
      completed_at: null,
      error_message: null,
    })).toBe(true);
  });

  it("treats terminal stages as inactive", () => {
    expect(isAgentSourceRunning({
      id: 3,
      status: "completed",
      current_stage: "completed",
      stage_message: null,
      plan_urls_count: 3,
      fetched_count: 3,
      quality_passed: 2,
      items_created: 2,
      started_at: null,
      completed_at: null,
      error_message: null,
    })).toBe(false);
    expect(isAgentSourceRunning({
      id: 4,
      status: "failed",
      current_stage: "failed",
      stage_message: null,
      plan_urls_count: 3,
      fetched_count: 1,
      quality_passed: 0,
      items_created: 0,
      started_at: null,
      completed_at: null,
      error_message: "boom",
    })).toBe(false);
    expect(isAgentSourceRunning(null)).toBe(false);
  });
});

describe("agent warmup run", () => {
  it("keeps warmup active until a real run record replaces the placeholder", () => {
    const placeholder = buildAgentWarmupRun();

    expect(placeholder.id).toBe(0);
    expect(placeholder.current_stage).toBe("planning");
    expect(placeholder.stage_message).toBe("正在创建运行记录");
    expect(isAgentSourceRunning(placeholder)).toBe(true);
    expect(isAgentWarmupResolved(placeholder)).toBe(false);
    expect(isAgentWarmupResolved({ ...placeholder, id: 9 })).toBe(true);
  });
});

describe("agentCandidateRunRefreshKeys", () => {
  it("includes the newly created agent source run query", () => {
    expect(agentCandidateRunRefreshKeys(83)).toEqual([
      ["agent-sources"],
      ["agent-source-candidates"],
      ["agent-runs", 83],
      ["items"],
    ]);
  });
});

describe("filterDemoItems", () => {
  it("filters demo items by search text and facets together", () => {
    const filtered = filterDemoItems(demoItems, {
      q: "kernel",
      main_category: "OS性能发展",
      info_type: "性能数据",
      importance: "高",
      sub_tag: "scheduler",
    });

    expect(filtered).toHaveLength(1);
    expect(filtered[0]?.title).toContain("Kernel");
  });

  it("supports multi-select category/importance/sub_tag filters", () => {
    const filtered = filterDemoItems(demoItems, {
      main_category: "OS性能发展,OS跟踪来源",
      importance: "高,中",
      sub_tag: "scheduler,security",
    });
    expect(filtered.map((item) => item.id).sort()).toEqual([1001, 1002]);
  });
});

describe("buildDemoFacets", () => {
  it("builds facet counts from demo items", () => {
    const facets = buildDemoFacets(demoItems);

    expect(facets.main_category.find((facet) => facet.value === "OS性能发展")?.count).toBe(1);
    expect(facets.info_type.find((facet) => facet.value === "适配")?.count).toBe(1);
    expect(facets.importance.find((facet) => facet.value === "高")?.count).toBeGreaterThan(0);
    expect(facets.sub_tags.find((facet) => facet.value === "kernel")?.count).toBe(1);
  });
});
