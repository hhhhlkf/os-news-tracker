import { useMemo, useState } from "react";
import type { CrawlMethod, Facets } from "../types";

interface Props {
  facets: Facets;
  crawlMethods?: CrawlMethod[];
  isLoading?: boolean;
  isMethodsLoading?: boolean;
  selected: Record<string, string>;
  onSelect: (key: string, value: string) => void;
}

type TimePreset = "all" | "24h" | "7d" | "30d" | "custom";

const HOTSPOT_SCROLL_HEIGHT = 128;
const SOURCE_SCROLL_HEIGHT = 184;

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

function methodDisplayName(method: CrawlMethod): string {
  return method.source_name?.trim() || method.domain || method.entry_url || `来源#${method.source_id ?? method.id}`;
}

export function FacetSidebar({ facets, crawlMethods = [], isLoading, isMethodsLoading, selected, onSelect }: Props) {
  const [customExpanded, setCustomExpanded] = useState(false);
  const [queryCustomExpanded, setQueryCustomExpanded] = useState(false);
  const [timeTab, setTimeTab] = useState<"published" | "fetched">("published");
  const activeCrawlMethods = useMemo(
    () => crawlMethods.filter((method) => method.status === "active"),
    [crawlMethods],
  );

  function resolveTimePreset(prefix: "published" | "fetched"): TimePreset {
    const afterMode = selected[`${prefix}_after_mode`];
    const afterValue = selected[`${prefix}_after_value`];
    const hasAbsoluteRange = !!(selected[`${prefix}_after`] || selected[`${prefix}_before`]);
    if (afterMode === "relative" && afterValue === "24h" && !selected[`${prefix}_before`]) return "24h";
    if (afterMode === "relative" && afterValue === "7d" && !selected[`${prefix}_before`]) return "7d";
    if (afterMode === "relative" && afterValue === "30d" && !selected[`${prefix}_before`]) return "30d";
    if (
      !hasAbsoluteRange
      && !afterMode
      && !afterValue
      && !selected[`${prefix}_before_mode`]
      && !selected[`${prefix}_before_value`]
    ) return "all";
    return "custom";
  }

  const activePreset: TimePreset = useMemo(() => resolveTimePreset("published"), [
    selected.published_after,
    selected.published_after_mode,
    selected.published_after_value,
    selected.published_before,
    selected.published_before_mode,
    selected.published_before_value,
  ]);

  const activeQueryPreset: TimePreset = useMemo(() => resolveTimePreset("fetched"), [
    selected.fetched_after,
    selected.fetched_after_mode,
    selected.fetched_after_value,
    selected.fetched_before,
    selected.fetched_before_mode,
    selected.fetched_before_value,
  ]);

  function handleTimePresetClick(prefix: "published" | "fetched", preset: TimePreset) {
    const setExpanded = prefix === "published" ? setCustomExpanded : setQueryCustomExpanded;
    if (preset === "all") {
      onSelect(`${prefix}_after_mode`, "");
      onSelect(`${prefix}_after_value`, "");
      onSelect(`${prefix}_after`, "");
      onSelect(`${prefix}_before_mode`, "");
      onSelect(`${prefix}_before_value`, "");
      onSelect(`${prefix}_before`, "");
      setExpanded(false);
    } else if (preset === "24h") {
      onSelect(`${prefix}_after_mode`, "relative");
      onSelect(`${prefix}_after_value`, "24h");
      onSelect(`${prefix}_after`, "");
      onSelect(`${prefix}_before_mode`, "");
      onSelect(`${prefix}_before_value`, "");
      onSelect(`${prefix}_before`, "");
      setExpanded(false);
    } else if (preset === "7d") {
      onSelect(`${prefix}_after_mode`, "relative");
      onSelect(`${prefix}_after_value`, "7d");
      onSelect(`${prefix}_after`, "");
      onSelect(`${prefix}_before_mode`, "");
      onSelect(`${prefix}_before_value`, "");
      onSelect(`${prefix}_before`, "");
      setExpanded(false);
    } else if (preset === "30d") {
      onSelect(`${prefix}_after_mode`, "relative");
      onSelect(`${prefix}_after_value`, "30d");
      onSelect(`${prefix}_after`, "");
      onSelect(`${prefix}_before_mode`, "");
      onSelect(`${prefix}_before_value`, "");
      onSelect(`${prefix}_before`, "");
      setExpanded(false);
    } else {
      // custom — toggle expansion
      setExpanded((prev) => !prev);
    }
  }

  function renderTimeControls(prefix: "published" | "fetched", active: TimePreset, expanded: boolean) {
    return (
      <>
        <div style={{ display: "flex", gap: 5, flexWrap: "wrap", marginBottom: expanded ? 10 : 0 }}>
          {presets.map(({ key, label }) => (
            <button
              key={key}
              onClick={() => handleTimePresetClick(prefix, key)}
              style={{
                border: "1px solid #d0d5dd",
                borderRadius: 6,
                padding: "5px 8px",
                fontSize: 12,
                cursor: "pointer",
                background: active === key ? "#eff8ff" : "transparent",
                color: active === key ? "#175cd3" : "#344054",
              }}
            >
              {label}
            </button>
          ))}
        </div>
        {expanded && (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <label style={{ color: "#475467", fontSize: 12 }}>开始日期</label>
              <input
                type="date"
                value={selected[`${prefix}_after`] ?? ""}
                onChange={(e) => {
                  onSelect(`${prefix}_after_mode`, e.target.value ? "absolute" : "");
                  onSelect(`${prefix}_after_value`, "");
                  onSelect(`${prefix}_after`, e.target.value);
                }}
                style={{
                  border: "1px solid #d0d5dd",
                  borderRadius: 7,
                  padding: "8px 10px",
                  fontSize: 13,
                  color: "#101828",
                  background: "#fff",
                  minWidth: 0,
                }}
              />
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
              <label style={{ color: "#475467", fontSize: 12 }}>结束日期</label>
              <input
                type="date"
                value={selected[`${prefix}_before`] ?? ""}
                onChange={(e) => {
                  onSelect(`${prefix}_before_mode`, e.target.value ? "absolute" : "");
                  onSelect(`${prefix}_before_value`, "");
                  onSelect(`${prefix}_before`, e.target.value);
                }}
                style={{
                  border: "1px solid #d0d5dd",
                  borderRadius: 7,
                  padding: "8px 10px",
                  fontSize: 13,
                  color: "#101828",
                  background: "#fff",
                  minWidth: 0,
                }}
              />
            </div>
          </div>
        )}
      </>
    );
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
            marginBottom: 10,
            border: "1px solid #d0d5dd",
            borderRadius: 8,
            padding: 10,
            background: "#fff",
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 7, color: "#101828", fontSize: 13 }}>{label}</div>
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
                       padding: "5px 8px",
                       borderRadius: 6,
                       background: active ? "#eff8ff" : "transparent",
                       color: active ? "#175cd3" : "#344054",
                       fontSize: 12,
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
                    <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.value}</span>
                  </span>
                  <span style={{ color: "#98a2b3" }}>{f.count}</span>
                </div>
              );
            })}
          </div>
        </section>
      ))}

      {/* ── Time filter ── */}
      <section
        style={{
          marginBottom: 10,
          border: "1px solid #d0d5dd",
          borderRadius: 8,
          padding: 12,
          background: "#fff",
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 8, color: "#101828", fontSize: 13 }}>时间筛选</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 4, marginBottom: 8 }}>
          {([
            ["published", "发布时间"],
            ["fetched", "查询时间"],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setTimeTab(key)}
              style={{
                border: "1px solid #d0d5dd",
                borderRadius: 6,
                padding: "6px 8px",
                fontSize: 12,
                fontWeight: 700,
                cursor: "pointer",
                background: timeTab === key ? "#175cd3" : "#fff",
                color: timeTab === key ? "#fff" : "#344054",
              }}
            >
              {label}
            </button>
          ))}
        </div>
        {timeTab === "published"
          ? renderTimeControls("published", activePreset, customExpanded)
          : renderTimeControls("fetched", activeQueryPreset, queryCustomExpanded)}
      </section>

      <section
        style={{
          marginBottom: 10,
          border: "1px solid #d0d5dd",
          borderRadius: 8,
          padding: 10,
          background: "#fff",
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 9, color: "#101828", fontSize: 13 }}>查询链接筛选</div>
        {isMethodsLoading ? (
          <div style={{ color: "#667085", fontSize: 12 }}>正在加载来源…</div>
        ) : activeCrawlMethods.length === 0 ? (
          <div style={{ color: "#98a2b3", fontSize: 12 }}>暂无可筛选来源</div>
        ) : (
          <div style={{ maxHeight: SOURCE_SCROLL_HEIGHT, overflowY: "auto", paddingRight: 5 }}>
            {activeCrawlMethods.map((method) => {
              const sourceId = method.source_id != null ? String(method.source_id) : "";
              if (!sourceId) return null;
              const selectedValues = parseFacetValues(selected.source_id);
              const active = selectedValues.includes(sourceId);
              const name = methodDisplayName(method);
              return (
                <div
                  key={`${method.id}:${sourceId}`}
                  onClick={() => onSelect("source_id", toggleFacetValue(selected.source_id, sourceId))}
                  title={name}
                  style={{
                    cursor: "pointer",
                    padding: "7px 9px",
                    borderRadius: 7,
                    background: active ? "#eff8ff" : "transparent",
                    color: active ? "#175cd3" : "#344054",
                    fontSize: 12,
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    lineHeight: 1.35,
                    minWidth: 0,
                  }}
                >
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
                      fontSize: 9,
                      lineHeight: 1,
                      flexShrink: 0,
                    }}
                  >
                    {active ? "✓" : ""}
                  </span>
                  <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</span>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </aside>
  );
}
