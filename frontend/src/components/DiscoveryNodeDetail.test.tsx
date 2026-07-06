// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { DiscoveryNodeDetail } from "./DiscoveryNodeDetail";

(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("DiscoveryNodeDetail", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    container.remove();
  });

  it("makes the summary area vertically scrollable for long content", async () => {
    await act(async () => {
      root.render(
        <DiscoveryNodeDetail
          nodeId="explorer"
          entry={{
            step: "explorer",
            status: "done",
            ts: "2026-07-02T00:00:00Z",
            summary: {
              list_url: "https://example.com/api/blog/list",
              notes: "line\n".repeat(80),
            },
          }}
        />,
      );
    });

    const summaryBox = container.querySelector("[data-testid='discovery-node-summary']");
    expect(summaryBox?.getAttribute("style")).toContain("overflow-y: auto");
    expect(summaryBox?.getAttribute("style")).toContain("max-height");
  });

  it("renders auditor as compact pass/fail summary", async () => {
    await act(async () => {
      root.render(
        <DiscoveryNodeDetail
          nodeId="auditor"
          entry={{
            step: "auditor",
            status: "done",
            ts: "2026-07-02T00:00:00Z",
            summary: {
              passed: false,
              decision: "reexplore",
              issues: ["抓取条目数 0", "分页请求超时"],
              attempt: 2,
              dsl_cycle_attempt: 0,
            },
          }}
        />,
      );
    });

    expect(container.textContent).toContain("审计失败");
    expect(container.textContent).toContain("抓取条目数 0");
    expect(container.textContent).toContain("分页请求超时");
    expect(container.textContent).not.toContain("dsl_cycle_attempt");
    expect(container.textContent).not.toContain("attempt");
    expect(container.textContent).not.toContain("decision");
  });
});
