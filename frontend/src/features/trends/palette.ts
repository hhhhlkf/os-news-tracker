export interface TrendColorGroup {
  background: string;
  color: string;
  border: string;
  line: string;
}

/**
 * Stable direction colors. The same label always resolves to the same group,
 * so templates may add or remove directions without reassigning old results.
 */
const DIRECTION_COLOR_GROUPS: readonly TrendColorGroup[] = [
  { background: "#eff8ff", color: "#175cd3", border: "#b2ddff", line: "#d1e9ff" },
  { background: "#f4f3ff", color: "#5925dc", border: "#d9d6fe", line: "#e9e7fe" },
  { background: "#ecfdf3", color: "#027a48", border: "#abefc6", line: "#d1fadf" },
  { background: "#fffaeb", color: "#b54708", border: "#fedf89", line: "#fef0c7" },
  { background: "#fff1f3", color: "#c01048", border: "#fecdd6", line: "#ffe4e8" },
  { background: "#eef4ff", color: "#3538cd", border: "#c7d7fe", line: "#e0eaff" },
  { background: "#ecfdff", color: "#0e7090", border: "#a5f0fc", line: "#cff9fe" },
  { background: "#fff6ed", color: "#c4320a", border: "#fed7aa", line: "#ffead5" },
];

const CATEGORY_COLOR_GROUPS: Record<string, TrendColorGroup> = {
  emerging_trend: DIRECTION_COLOR_GROUPS[1],
  hot_event: { background: "#fef3f2", color: "#b42318", border: "#fecdca", line: "#fee4e2" },
  periodic_activity: DIRECTION_COLOR_GROUPS[0],
  attention_declining: { background: "#f2f4f7", color: "#475467", border: "#d0d5dd", line: "#eaecf0" },
  unverified_change: { background: "#f2f4f7", color: "#667085", border: "#d0d5dd", line: "#eaecf0" },
};

const FALLBACK_CATEGORY_COLOR: TrendColorGroup = { background: "#f2f4f7", color: "#667085", border: "#d0d5dd", line: "#eaecf0" };

function stableColorIndex(label: string): number {
  let hash = 0;
  for (const char of Array.from(label.trim())) {
    hash = ((hash * 31) + (char.codePointAt(0) ?? 0)) >>> 0;
  }
  return hash % DIRECTION_COLOR_GROUPS.length;
}

export function trendDirectionColor(direction: string | null | undefined): TrendColorGroup {
  if (!direction?.trim()) return FALLBACK_CATEGORY_COLOR;
  return DIRECTION_COLOR_GROUPS[stableColorIndex(direction)];
}

export function trendCategoryColor(category: string | null | undefined): TrendColorGroup {
  return CATEGORY_COLOR_GROUPS[category ?? ""] ?? FALLBACK_CATEGORY_COLOR;
}

/** Keep the familiar OS → AI sequence while leaving other dynamic labels in author order. */
export function trendDirectionSortRank(direction: string): number {
  const normalized = direction.trim().toLowerCase();
  if (normalized.includes("os") || normalized.includes("操作系统")) return 0;
  if (normalized.includes("ai") || normalized.includes("人工智能")) return 1;
  return 2;
}
