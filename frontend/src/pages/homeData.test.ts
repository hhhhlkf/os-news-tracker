import { describe, expect, it } from "vitest";
import { buildDemoFacets, filterDemoItems, resolveHomeDataMode } from "./homeData";
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

describe("filterDemoItems", () => {
  it("filters demo items by search text and facets together", () => {
    const filtered = filterDemoItems(demoItems, {
      q: "kernel",
      main_category: "OS性能发展",
      info_type: "性能数据",
      importance: "高",
    });

    expect(filtered).toHaveLength(1);
    expect(filtered[0]?.title).toContain("Kernel");
  });
});

describe("buildDemoFacets", () => {
  it("builds facet counts from demo items", () => {
    const facets = buildDemoFacets(demoItems);

    expect(facets.main_category.find((facet) => facet.value === "OS性能发展")?.count).toBe(1);
    expect(facets.info_type.find((facet) => facet.value === "适配")?.count).toBe(1);
    expect(facets.importance.find((facet) => facet.value === "高")?.count).toBeGreaterThan(0);
  });
});
