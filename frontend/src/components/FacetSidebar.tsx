import { useMemo, useState } from "react";
import type { Facets } from "../types";

interface Props {
  facets: Facets;
  isLoading?: boolean;
  selected: Record<string, string>;
  onSelect: (key: string, value: string) => void;
}

type TimePreset = "all" | "24h" | "7d" | "30d" | "custom";

const HOTSPOT_SCROLL_HEIGHT = 164;

export function getFacetFilterKey(key: keyof Facets): string {
  return key === "sub_tags" ? "sub_tag" : key;
}

export function getFacetGroups(): [keyof Facets, string][] {
  return [
    ["main_category", "主分类"],
    ["importance", "重要度"],
    ["sub_tags", "技术热点"],
  ];
}

/** Parse comma-separated multi-select facet values. */
export function parseFacetValues(raw: string | undefined | null): string[] {
  if (!raw) return [];
  const values: string[] = [];
  const seen = new Set<string>();
  for (const part of raw.split(",")) {
    const value = part.trim();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    values.push(value);
  }
  return values;
}

/** Toggle one value in a comma-separated multi-select filter string. */
export function toggleFacetValue(raw: string | undefined | null, value: string): string {
  const current = parseFacetValues(raw);
  if (current.includes(value)) {
    return current.filter((item) => item !== value).join(",");
  }
  return [...current, value].join(",");
}

export function FacetSidebar({ facets, isLoading, selected, onSelect }: Props) {
  const [customExpanded, setCustomExpanded] = useState(false);

  const activePreset: TimePreset = useMemo(() => {
    const afterMode = selected.published_after_mode;
    const afterValue = selected.published_after_value;
    const hasAbsoluteRange = !!(selected.published_after || selected.published_before);
    if (afterMode === "relative" && afterValue === "24h" && !selected.published_before) return "24h";
    if (afterMode === "relative" && afterValue === "7d" && !selected.published_before) return "7d";
    if (afterMode === "relative" && afterValue === "30d" && !selected.published_before) return "30d";
    if (!hasAbsoluteRange && !afterMode && !afterValue && !selected.published_before_mode && !selected.published_before_value) return "all";
    return "custom";
  }, [
    selected.published_after,
    selected.published_after_mode,
    selected.published_after_value,
    selected.published_before,
    selected.published_before_mode,
    selected.published_before_value,
  ]);

  function handlePresetClick(preset: TimePreset) {
    if (preset === "all") {
      onSelect("published_after_mode", "");
      onSelect("published_after_value", "");
      onSelect("published_after", "");
      onSelect("published_before_mode", "");
      onSelect("published_before_value", "");
      onSelect("published_before", "");
      setCustomExpanded(false);
    } else if (preset === "24h") {
      onSelect("published_after_mode", "relative");
      onSelect("published_after_value", "24h");
      onSelect("published_after", "");
      onSelect("published_before_mode", "");
      onSelect("published_before_value", "");
      onSelect("published_before", "");
      setCustomExpanded(false);
    } else if (preset === "7d") {
      onSelect("published_after_mode", "relative");
      onSelect("published_after_value", "7d");
      onSelect("published_after", "");
      onSelect("published_before_mode", "");
      onSelect("published_before_value", "");
      onSelect("published_before", "");
      setCustomExpanded(false);
    } else if (preset === "30d") {
      onSelect("published_after_mode", "relative");
      onSelect("published_after_value", "30d");
      onSelect("published_after", "");
      onSelect("published_before_mode", "");
      onSelect("published_before_value", "");
      onSelect("published_before", "");
      setCustomExpanded(false);
    } else {
      // custom — toggle expansion
      setCustomExpanded((prev) => !prev);
    }
  }

  if (isLoading) {
    return (
      <aside style={{ width: "100%" }}>
        <div style={{ color: "#667085", fontSize: 14 }}>正在加载筛选项…</div>
      </aside>
    );
  }

  const groups = getFacetGroups();

  const presets: { key: TimePreset; label: string }[] = [
    { key: "all", label: "全部" },
    { key: "24h", label: "24h" },
    { key: "7d", label: "7天" },
    { key: "30d", label: "30天" },
    { key: "custom", label: "自定义" },
  ];

  return (
    <aside style={{ width: "100%" }}>
      {groups.map(([key, label]) => (
        <section
          key={key}
          style={{
            marginBottom: 16,
            border: "1px solid #d0d5dd",
            borderRadius: 8,
            padding: 14,
            background: "#fff",
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 10, color: "#101828" }}>{label}</div>
          <div
            style={key === "sub_tags" ? {
              maxHeight: HOTSPOT_SCROLL_HEIGHT,
              overflowY: "auto",
              paddingRight: 4,
            } : undefined}
          >
            {facets[key].map((f: { value: string; count: number }) => {
              const filterKey = getFacetFilterKey(key);
              const selectedValues = parseFacetValues(selected[filterKey]);
              const active = selectedValues.includes(f.value);
              return (
                <div key={f.value}
                     onClick={() => onSelect(filterKey, toggleFacetValue(selected[filterKey], f.value))}
                     style={{
                       cursor: "pointer",
                       padding: "8px 10px",
                       borderRadius: 6,
                       background: active ? "#eff8ff" : "transparent",
                       color: active ? "#175cd3" : "#344054",
                       fontSize: 13,
                       display: "flex",
                       justifyContent: "space-between",
                       alignItems: "center",
                       gap: 8,
                     }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                    <span
                      aria-hidden
                      style={{
                        width: 14,
                        height: 14,
                        borderRadius: 3,
                        border: active ? "1px solid #175cd3" : "1px solid #d0d5dd",
                        background: active ? "#175cd3" : "#fff",
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                        color: "#fff",
                        fontSize: 10,
                        lineHeight: 1,
                        flexShrink: 0,
                      }}
                    >
                      {active ? "✓" : ""}
                    </span>
                    <span>{f.value}</span>
                  </span>
                  <span style={{ color: "#98a2b3" }}>{f.count}</span>
                </div>
              );
            })}
          </div>
        </section>
      ))}

      {/* ── Published time filter ── */}
      <section
        style={{
          marginBottom: 16,
          border: "1px solid #d0d5dd",
          borderRadius: 8,
          padding: 14,
          background: "#fff",
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 10, color: "#101828" }}>发布时间</div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: customExpanded ? 12 : 0 }}>
          {presets.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => handlePresetClick(key)}
              style={{
                border: "1px solid #d0d5dd",
                borderRadius: 6,
                padding: "6px 10px",
                fontSize: 12,
                cursor: "pointer",
                background: activePreset === key ? "#eff8ff" : "transparent",
                color: activePreset === key ? "#175cd3" : "#344054",
              }}
            >
              {label}
            </button>
          ))}
        </div>
        {customExpanded && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ color: "#475467", fontSize: 13 }}>开始日期</label>
              <input
                type="date"
                value={selected.published_after ?? ""}
                onChange={(e) => {
                  onSelect("published_after_mode", e.target.value ? "absolute" : "");
                  onSelect("published_after_value", "");
                  onSelect("published_after", e.target.value);
                }}
                style={{
                  border: "1px solid #d0d5dd",
                  borderRadius: 8,
                  padding: "10px 12px",
                  fontSize: 14,
                  color: "#101828",
                  background: "#fff",
                  minWidth: 160,
                }}
              />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <label style={{ color: "#475467", fontSize: 13 }}>结束日期</label>
              <input
                type="date"
                value={selected.published_before ?? ""}
                onChange={(e) => {
                  onSelect("published_before_mode", e.target.value ? "absolute" : "");
                  onSelect("published_before_value", "");
                  onSelect("published_before", e.target.value);
                }}
                style={{
                  border: "1px solid #d0d5dd",
                  borderRadius: 8,
                  padding: "10px 12px",
                  fontSize: 14,
                  color: "#101828",
                  background: "#fff",
                  minWidth: 160,
                }}
              />
            </div>
          </div>
        )}
      </section>
    </aside>
  );
}
