import { describe, expect, it } from "vitest";
import { getNodeBoxVisualStyle, getEdgeLabel } from "./DiscoveryFlowChart";

describe("getNodeBoxVisualStyle", () => {
  it("gives running nodes a pulse animation", () => {
    const style = getNodeBoxVisualStyle({
      state: "running",
      isAgent: true,
    });

    expect(style.animation).toContain("discovery-node-pulse");
    expect(style.boxShadow).toContain("rgba(23,92,211");
  });

  it("keeps done nodes static", () => {
    const style = getNodeBoxVisualStyle({
      state: "done",
      isAgent: false,
    });

    expect(style.animation).toBeUndefined();
  });

  it("renders warning nodes in yellow without pulse animation", () => {
    const style = getNodeBoxVisualStyle({
      state: "warning",
      isAgent: true,
    });

    expect(style.animation).toBeUndefined();
    expect(style.borderColor).toBe("#f59e0b");
    expect(style.background).toBe("#fffaeb");
  });

  it("labels local rewrite loop with cycle round", () => {
    expect(getEdgeLabel("auditor->dsl_writer", 2)).toBe("第 2 / 3 轮");
  });
});
