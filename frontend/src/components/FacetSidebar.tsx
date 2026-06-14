import { useMemo, useState } from "react";
import type { Facets } from "../types";

interface Props {
  facets: Facets;
  isLoading?: boolean;
  selected: Record<string, string>;
  onSelect: (key: string, value: string) => void;
}

function daysAgo(n: number): string {
  const d = new Date();
  d.setDate(d.getDate() - n);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
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

export function FacetSidebar({ facets, isLoading, selected, onSelect }: Props) {
  const [customExpanded, setCustomExpanded] = useState(false);

  const activePreset: TimePreset = useMemo(() => {
    const after = selected.published_after;
    const before = selected.published_before;
    if (!after && !before) return "all";
    if (after === daysAgo(1) && !before) return "24h";
    if (after === daysAgo(7) && !before) return "7d";
    if (after === daysAgo(30) && !before) return "30d";
    return "custom";
  }, [selected.published_after, selected.published_before]);

  function handlePresetClick(preset: TimePreset) {
    if (preset === "all") {
      onSelect("published_after", "");
      onSelect("published_before", "");
      setCustomExpanded(false);
    } else if (preset === "24h") {
      onSelect("published_after", daysAgo(1));
      onSelect("published_before", "");
      setCustomExpanded(false);
    } else if (preset === "7d") {
      onSelect("published_after", daysAgo(7));
      onSelect("published_before", "");
      setCustomExpanded(false);
    } else if (preset === "30d") {
      onSelect("published_after", daysAgo(30));
      onSelect("published_before", "");
      setCustomExpanded(false);
    } else {
      // custom — toggle expansion
      setCustomExpanded((prev) => !prev);
    }
  }

  if (isLoading) {
    return (
      <aside style={{ width: 260, paddingRight: 20 }}>
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
    <aside style={{ width: 260, paddingRight: 20, flexShrink: 0 }}>
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
              const activeValue = selected[filterKey];
              return (
                <div key={f.value}
                     onClick={() => onSelect(filterKey, activeValue === f.value ? "" : f.value)}
                     style={{
                       cursor: "pointer",
                       padding: "8px 10px",
                       borderRadius: 6,
                       background: activeValue === f.value ? "#eff8ff" : "transparent",
                       color: activeValue === f.value ? "#175cd3" : "#344054",
                       fontSize: 13,
                       display: "flex",
                       justifyContent: "space-between",
                     }}>
                  <span>{f.value}</span>
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
                onChange={(e) => onSelect("published_after", e.target.value)}
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
                onChange={(e) => onSelect("published_before", e.target.value)}
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
