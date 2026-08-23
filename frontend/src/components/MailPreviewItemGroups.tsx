import type { CSSProperties } from "react";
import type { MailPreviewItem } from "../mail/types";
import { MailPreviewItemCard } from "./MailPreviewItemCard";

interface Props {
  items: MailPreviewItem[];
  compact?: boolean;
}

interface Group {
  title: string;
  color: string;
  background: string;
  border: string;
  items: MailPreviewItem[];
}

const mainCategoryColors = [
  { color: "#175cd3", background: "#eff8ff", border: "#b2ddff" },
  { color: "#5925dc", background: "#f4f3ff", border: "#d9d6fe" },
  { color: "#027a48", background: "#ecfdf3", border: "#abefc6" },
  { color: "#b54708", background: "#fffaeb", border: "#fedf89" },
  { color: "#c01048", background: "#fff1f3", border: "#fecdd6" },
  { color: "#0e7090", background: "#ecfdff", border: "#a5f0fc" },
] as const;

function isAiToolsCategory(category: string): boolean {
  return category.includes("AI工具");
}

export function groupMailPreviewItems(items: MailPreviewItem[]): Group[] {
  const byCategory = new Map<string, MailPreviewItem[]>();
  for (const item of items) {
    const category = item.main_category?.trim() || "未分类";
    byCategory.set(category, [...(byCategory.get(category) ?? []), item]);
  }
  return [...byCategory.entries()]
    .sort(([left], [right]) => {
      const rankDifference = Number(isAiToolsCategory(left)) - Number(isAiToolsCategory(right));
      return rankDifference || left.localeCompare(right, "zh-CN");
    })
    .map(([title, categoryItems], index) => ({
      title,
      ...mainCategoryColors[index % mainCategoryColors.length],
      items: categoryItems,
    }));
}

export function MailPreviewItemGroups({ items, compact = false }: Props) {
  return (
    <>
      {groupMailPreviewItems(items).map((group) => (
        <section key={group.title} style={{ display: "grid", gap: compact ? 8 : 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 9, padding: "2px 0" }}>
            <span style={{ ...groupLabel, color: group.color, background: group.background, borderColor: group.border }}>{group.title}</span>
            <span style={{ height: 1, flex: 1, background: group.border }} />
            <span style={{ color: "#98a2b3", fontSize: 13, whiteSpace: "nowrap" }}>{group.items.length} 条</span>
          </div>
          <div style={timelineList}>
            <div aria-hidden style={{ ...timelineLine, background: group.color }} />
            {group.items.map((item, index) => (
              <div key={`${item.source_url}-${index}`} style={timelineRow}>
                <div aria-hidden style={{ ...timelineDot, background: group.color, borderColor: "#f8fafc" }} />
                <MailPreviewItemCard item={item} compact={compact} />
              </div>
            ))}
          </div>
        </section>
      ))}
    </>
  );
}

const groupLabel: CSSProperties = {
  border: "1px solid",
  borderRadius: 999,
  padding: "4px 9px",
  fontSize: 12,
  fontWeight: 800,
  whiteSpace: "nowrap",
};
const timelineList: CSSProperties = { position: "relative", display: "grid", gap: 14 };
const timelineLine: CSSProperties = { position: "absolute", top: 0, bottom: 0, left: 11, width: 1 };
const timelineRow: CSSProperties = { position: "relative", paddingLeft: 24 };
const timelineDot: CSSProperties = { position: "absolute", top: 18, left: 6, width: 11, height: 11, border: "2px solid", borderRadius: "50%", boxSizing: "border-box", zIndex: 1 };
