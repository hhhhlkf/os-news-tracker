import type { CSSProperties } from "react";
import type { MailTrendDirectionGroup } from "../mail/types";
import { trendCategoryColor, trendDirectionColor, trendDirectionSortRank } from "../features/trends/palette";
import { MailPreviewItemCard } from "./MailPreviewItemCard";

export function MailTrendPreviewGroups({ groups }: { groups: MailTrendDirectionGroup[] }) {
  return (
    <div style={{ display: "grid", gap: 16 }}>
      {[...groups].sort((left, right) => trendDirectionSortRank(left.direction) - trendDirectionSortRank(right.direction)).map((group) => <TrendDirectionGroup key={group.direction} group={group} />)}
    </div>
  );
}

function TrendDirectionGroup({ group }: { group: MailTrendDirectionGroup }) {
  const directionColor = trendDirectionColor(group.direction);
  return (
    <section style={{ display: "grid", gap: 9 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span
          style={{
            border: `1px solid ${directionColor.border}`,
            borderRadius: 999,
            background: directionColor.background,
            color: directionColor.color,
            padding: "4px 9px",
            fontSize: 12,
            fontWeight: 800,
            whiteSpace: "nowrap",
          }}
        >
          {group.direction}
        </span>
        <span style={{ height: 1, flex: 1, background: directionColor.line }} />
        <span style={{ fontSize: 13, color: "#98a2b3", whiteSpace: "nowrap" }}>{group.trends.length} 条趋势</span>
      </div>
      <div style={timelineList}>
        <div aria-hidden style={{ ...timelineLine, background: directionColor.color }} />
        {group.trends.map((trend) => (
          <div key={trend.result_id} style={timelineRow}>
            <div aria-hidden style={{ ...timelineDot, background: directionColor.color, borderColor: "#f8fafc" }} />
            <TrendSummary trend={trend} />
          </div>
        ))}
      </div>
    </section>
  );
}

const timelineList: CSSProperties = { position: "relative", display: "grid", gap: 14 };
const timelineLine: CSSProperties = { position: "absolute", top: 0, bottom: 0, left: 11, width: 1 };
const timelineRow: CSSProperties = { position: "relative", paddingLeft: 24 };
const timelineDot: CSSProperties = { position: "absolute", top: 18, left: 6, width: 11, height: 11, border: "2px solid", borderRadius: "50%", boxSizing: "border-box", zIndex: 1 };

function TrendSummary({ trend }: { trend: MailTrendDirectionGroup["trends"][number] }) {
  const categoryColor = trendCategoryColor(trend.category);
  return (
    <details style={{ border: "1px solid #d0d5dd", borderRadius: 12, background: "#fff", padding: "11px 12px" }}>
              <summary style={{ listStyle: "none", cursor: "pointer" }}>
                <div style={{ display: "flex", gap: 10, alignItems: "flex-start", justifyContent: "space-between" }}>
                  <strong style={{ minWidth: 0, flex: 1, fontSize: 14, lineHeight: 1.45, color: "#101828" }}>{trend.title}</strong>
                  <span
                    style={{
                      flexShrink: 0,
                      borderRadius: 999,
                      background: categoryColor.background,
                      color: categoryColor.color,
                      border: `1px solid ${categoryColor.border}`,
                      padding: "3px 7px",
                      fontSize: 11,
                      fontWeight: 700,
                      whiteSpace: "nowrap",
                    }}
                  >
                    {trend.category_label}
                  </span>
                </div>
                <div style={{ marginTop: 6, color: "#475467", fontSize: 12, lineHeight: 1.65 }}>{trend.summary || "暂无趋势概括"}</div>
                <div style={{ marginTop: 7, fontSize: 11, color: "#98a2b3" }}>关联新闻 {trend.sources.length} 条 · 点击展开完整新闻卡片</div>
              </summary>
              <div style={{ display: "grid", gap: 8, marginTop: 11 }}>
                {trend.sources.length > 0 ? (
                  trend.sources.map((item, index) => (
                    <MailPreviewItemCard key={`${item.source_url}-${index}`} item={item} compact />
                  ))
                ) : (
                  <div style={{ fontSize: 12, color: "#98a2b3" }}>暂无关联新闻。</div>
                )}
              </div>
    </details>
  );
}
