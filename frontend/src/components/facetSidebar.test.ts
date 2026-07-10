import { describe, expect, it } from "vitest";
import {
  getFacetFilterKey,
  getFacetGroups,
  parseFacetValues,
  toggleFacetValue,
} from "./FacetSidebar";

describe("getFacetGroups", () => {
  it("omits info_type and keeps technical hotspots", () => {
    expect(getFacetGroups()).toEqual([
      ["main_category", "主分类"],
      ["importance", "重要度"],
      ["sub_tags", "技术热点"],
    ]);
  });
});

describe("getFacetFilterKey", () => {
  it("maps technical hotspots to the sub_tag filter param", () => {
    expect(getFacetFilterKey("sub_tags")).toBe("sub_tag");
  });

  it("keeps other facet keys unchanged", () => {
    expect(getFacetFilterKey("main_category")).toBe("main_category");
    expect(getFacetFilterKey("importance")).toBe("importance");
  });
});

describe("multi-select facet helpers", () => {
  it("parses and toggles comma-separated facet values", () => {
    expect(parseFacetValues("高,中")).toEqual(["高", "中"]);
    expect(toggleFacetValue("", "高")).toBe("高");
    expect(toggleFacetValue("高", "中")).toBe("高,中");
    expect(toggleFacetValue("高,中", "高")).toBe("中");
  });
});
