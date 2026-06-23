import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  NewsRunControl,
  buildNewsRunFormState,
  formatNewsRunWindowLabel,
  isNewsRunBusy,
  shouldAutoExpandNewsRunControl,
  toAbsoluteDateTime,
} from "../components/NewsRunControl";
import type { ManualNewsRunStatus } from "../types";

describe("buildNewsRunFormState", () => {
  it("prefills from relative status", () => {
    const status: ManualNewsRunStatus = {
      state: "completed",
      time_mode: "relative",
      relative_range: "7d",
      start_at: null,
      end_at: null,
      target_count: 80,
      discovered_count: 95,
      queued_count: 72,
      processed_count: 72,
      saved_count: 12,
      fulfilled: false,
      gap_reason: "候选不足",
      started_at: null,
      finished_at: null,
      last_error: null,
      time_filter_stats: null,
    };

    expect(buildNewsRunFormState(status)).toMatchObject({
      timeMode: "relative",
      relativeRange: "7d",
      targetCount: "80",
    });
  });
});

describe("formatNewsRunWindowLabel", () => {
  it("formats absolute ranges for display", () => {
    const status: ManualNewsRunStatus = {
      state: "collecting",
      time_mode: "absolute",
      relative_range: null,
      start_at: "2026-06-01T00:00:00",
      end_at: "2026-06-11T00:00:00",
      target_count: 50,
      discovered_count: 0,
      queued_count: 0,
      processed_count: 0,
      saved_count: 0,
      fulfilled: false,
      gap_reason: null,
      started_at: null,
      finished_at: null,
      last_error: null,
      time_filter_stats: null,
    };

    expect(formatNewsRunWindowLabel(status)).toContain("2026-06-01");
    expect(formatNewsRunWindowLabel(status)).toContain("2026-06-11");
  });
});

describe("isNewsRunBusy", () => {
  it("returns true for active states", () => {
    expect(isNewsRunBusy("collecting")).toBe(true);
    expect(isNewsRunBusy("processing")).toBe(true);
    expect(isNewsRunBusy("stopping")).toBe(true);
    expect(isNewsRunBusy("idle")).toBe(false);
    expect(isNewsRunBusy("completed")).toBe(false);
    expect(isNewsRunBusy("failed")).toBe(false);
  });
});

describe("shouldAutoExpandNewsRunControl", () => {
  it("expands automatically for active or failed runs", () => {
    expect(shouldAutoExpandNewsRunControl("collecting")).toBe(true);
    expect(shouldAutoExpandNewsRunControl("processing")).toBe(true);
    expect(shouldAutoExpandNewsRunControl("stopping")).toBe(true);
    expect(shouldAutoExpandNewsRunControl("failed")).toBe(true);
    expect(shouldAutoExpandNewsRunControl("idle")).toBe(false);
    expect(shouldAutoExpandNewsRunControl("completed")).toBe(false);
  });
});

describe("toAbsoluteDateTime", () => {
  it("emits explicit UTC start-of-day (Z suffix, not local time)", () => {
    const result = toAbsoluteDateTime("2026-06-04", false);
    // Must be explicit UTC midnight, not timezone-dependent.
    expect(result).toBe("2026-06-04T00:00:00.000Z");
  });

  it("emits explicit UTC end-of-day with millisecond precision", () => {
    const result = toAbsoluteDateTime("2026-06-11", true);
    expect(result).toBe("2026-06-11T23:59:59.999Z");
  });

  it("returns null for empty input", () => {
    expect(toAbsoluteDateTime("", false)).toBeNull();
    expect(toAbsoluteDateTime("", true)).toBeNull();
  });

  it("produces the same UTC string regardless of runtime timezone", () => {
    // The function must not call new Date() with a local-time string.
    // We verify by checking the output contains "Z" (explicit UTC marker).
    const start = toAbsoluteDateTime("2026-06-04", false);
    const end = toAbsoluteDateTime("2026-06-11", true);
    expect(start).toMatch(/Z$/);
    expect(end).toMatch(/Z$/);
    // The date part must match the input date, not be shifted by timezone.
    expect(start).toContain("2026-06-04");
    expect(end).toContain("2026-06-11");
  });
});

describe("buildNewsRunFormState with time_filter_stats", () => {
  it("shows the date value that matches what was submitted (UTC)", () => {
    const status: ManualNewsRunStatus = {
      state: "completed",
      time_mode: "absolute",
      relative_range: null,
      // Backend returns UTC ISO strings — frontend must display the UTC date.
      start_at: "2026-06-04T00:00:00.000Z",
      end_at: "2026-06-11T23:59:59.999Z",
      target_count: 50,
      discovered_count: 100,
      queued_count: 30,
      processed_count: 30,
      saved_count: 12,
      fulfilled: false,
      gap_reason: "入库不足",
      started_at: null,
      finished_at: null,
      last_error: null,
      time_filter_stats: null,
    };

    expect(buildNewsRunFormState(status)).toMatchObject({
      timeMode: "absolute",
      targetCount: "50",
    });
    // The date input values should reflect the UTC dates.
    expect(formatNewsRunWindowLabel(status)).toContain("2026-06-04");
    expect(formatNewsRunWindowLabel(status)).toContain("2026-06-11");
  });
});

describe("NewsRunControl mode switch shell", () => {
  const status: ManualNewsRunStatus = {
    state: "idle",
    time_mode: "relative",
    relative_range: "7d",
    start_at: null,
    end_at: null,
    target_count: 50,
    discovered_count: 0,
    queued_count: 0,
    processed_count: 0,
    saved_count: 0,
    fulfilled: false,
    gap_reason: null,
    started_at: null,
    finished_at: null,
    last_error: null,
    time_filter_stats: null,
  };

  it("defaults to standard mode and keeps manual control text", () => {
    const html = renderToStaticMarkup(
      createElement(NewsRunControl, {
        mode: "standard",
        status,
        isLoading: false,
        onStart: () => {},
        onStop: () => {},
      }),
    );

    expect(html).toContain("标准抓取");
    expect(html).toContain("开始处理");
    expect(html).not.toContain("Agent 模式内容");
  });

  it("keeps agent content collapsed by default when agent mode is selected", () => {
    const html = renderToStaticMarkup(
      createElement(NewsRunControl, {
        mode: "agent",
        agentContent: createElement("div", null, "Agent 模式内容"),
        sourceManagerContent: createElement("div", null, "来源管理内容"),
        status,
        isLoading: false,
        onStart: () => {},
        onStop: () => {},
      }),
    );

    expect(html).toContain("Agent Crawl");
    expect(html).toContain("展开设置");
    expect(html).not.toContain("Agent 模式内容");
    expect(html).not.toContain("来源管理内容");
    expect(html).not.toContain("开始处理");
  });

  it("renders source manager and agent content when agent mode auto-expands", () => {
    const failedStatus = { ...status, state: "failed" as const };
    const html = renderToStaticMarkup(
      createElement(NewsRunControl, {
        mode: "agent",
        agentContent: createElement("div", null, "Agent 模式内容"),
        sourceManagerContent: createElement("div", null, "来源管理内容"),
        status: failedStatus,
        isLoading: false,
        onStart: () => {},
        onStop: () => {},
      }),
    );

    expect(html).toContain("收起设置");
    expect(html).toContain("Agent 模式内容");
    expect(html).toContain("来源管理内容");
  });

  it("keeps agent mode entry visible by default", () => {
    const html = renderToStaticMarkup(
      createElement(NewsRunControl, {
        mode: "standard",
        status,
        isLoading: false,
        onStart: () => {},
        onStop: () => {},
      }),
    );

    expect(html).toContain("标准抓取");
    expect(html).toContain("Agent Crawl");
  });
});
