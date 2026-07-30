import { useState, type ReactNode } from "react";
import type { MailPreviewItem } from "../mail/types";
import { ImportanceBadge } from "./ImportanceBadge";
import { formatDateYmd, HotspotTags, SourceCta, SourceQualityMeta, TechHighlightsList } from "./ItemMetaBlocks";

function SourceRow({ item }: { item: MailPreviewItem }): ReactNode {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        flexWrap: "nowrap",
        minWidth: 0,
        overflow: "hidden",
      }}
    >
      {item.source_url ? <SourceCta url={item.source_url} size="sm" /> : null}
      <SourceQualityMeta
        sourceName={item.source_name}
        score={item.source_quality_score}
        grade={item.source_quality_grade}
        status={item.source_quality_status}
        size="sm"
      />
    </div>
  );
}

export function MailPreviewItemCard({
  item,
  compact = false,
}: {
  item: MailPreviewItem;
  /** Template preview uses a slightly denser layout. */
  compact?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const titleSize = compact ? 15 : 16;
  const pad = compact ? "10px 12px" : "12px 14px";

  return (
    <div style={{ background: "#fff", border: "1px solid #eaecf0", borderRadius: 12, padding: pad }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
        <div style={{ minWidth: 0, flex: 1, fontSize: titleSize, fontWeight: 800, color: "#101828", lineHeight: 1.4 }}>
          {item.title}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexShrink: 0, whiteSpace: "nowrap" }}>
          <ImportanceBadge value={item.importance} />
          <div style={{ fontSize: 11, color: "#667085" }}>{formatDateYmd(item.published_at)}</div>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            title={expanded ? "收起" : "展开"}
            style={{
              flexShrink: 0,
              border: "1px solid #d0d5dd",
              background: expanded ? "#f2f4f7" : "#fff",
              color: "#344054",
              borderRadius: 8,
              padding: "4px 10px",
              fontSize: 12,
              fontWeight: 700,
              cursor: "pointer",
              whiteSpace: "nowrap",
              lineHeight: 1.4,
            }}
          >
            {expanded ? "收起 ▴" : "展开 ▾"}
          </button>
        </div>
      </div>

      <div
        style={{
          marginTop: 6,
          fontSize: compact ? 12 : 13,
          color: "#475467",
          lineHeight: 1.65,
        }}
      >
        {item.summary?.trim() ? item.summary : "暂无摘要"}
      </div>

      {!expanded && (
        <div style={{ marginTop: 8 }}>
          <SourceRow item={item} />
        </div>
      )}

      {expanded && (
        <div style={{ display: "grid", gap: compact ? 8 : 10, fontSize: 13, color: "#475467", lineHeight: 1.7, marginTop: 10 }}>
          <div>
            {!compact && (
              <strong style={{ color: "#101828", display: "block", marginBottom: 6 }}>技术要点</strong>
            )}
            <TechHighlightsList items={item.key_points} compact />
          </div>
          <div>
            {!compact && (
              <strong style={{ color: "#101828", display: "block", marginBottom: 6 }}>技术热点</strong>
            )}
            <HotspotTags tags={item.hotspots} />
          </div>
          <SourceRow item={item} />
        </div>
      )}
    </div>
  );
}
