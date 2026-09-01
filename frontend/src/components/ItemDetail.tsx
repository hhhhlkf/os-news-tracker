import type { CSSProperties, ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchItemDetail, ApiError } from "../api/client";
import type { ItemDetail as ItemDetailRecord } from "../types";
import { fetchDiscussionDetail } from "../discussions/api";
import { DiscussionTopology } from "./DiscussionTopology";
import { ImportanceBadge } from "./ImportanceBadge";
import { InfoTypeBadge } from "./InfoTypeBadge";
import { formatDateYmd, HotspotTags, SourceCta, TechHighlightsList } from "./ItemMetaBlocks";

function ReaderShell({
  wide,
  children,
}: {
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <div
      className={`item-reader-sheet${wide ? " item-reader-sheet--discussion" : ""}`}
      onClick={(event) => event.stopPropagation()}
      role="dialog"
      aria-modal="true"
    >
      {children}
    </div>
  );
}

function ItemDetailBody({ data, onClose }: { data: ItemDetailRecord; onClose?: () => void }) {
  const dedupedSourceLinks = Array.from(
    new Map(data.source_links.map((s) => [`${s.source_id}:${s.url}`, s])).values(),
  );
  const isDiscussion = data.item_kind === "discussion";
  const discussionQuery = useQuery({
    queryKey: ["discussion-detail", data.id],
    queryFn: () => fetchDiscussionDetail(data.id),
    enabled: isDiscussion,
    retry: false,
  });
  const title = data.title_tldr?.trim() || data.title;
  const secondaryTitle = isDiscussion ? data.original_title : data.title;
  const showSecondary = Boolean(secondaryTitle && secondaryTitle !== title);
  const highlights = data.key_points.filter((kp) => !kp.startsWith("__type:"));
  const metaBits = [
    data.main_category,
    data.published_at ? `发布 ${formatDateYmd(data.published_at)}` : null,
    isDiscussion && data.last_activity_at ? `最近活动 ${formatDateYmd(data.last_activity_at)}` : null,
    !isDiscussion && data.fetched_at ? `入库 ${formatDateYmd(data.fetched_at)}` : null,
  ].filter(Boolean) as string[];

  return (
    <ReaderShell wide={isDiscussion}>
      <header style={toolbar}>
        <span style={isDiscussion ? kindDiscussion : kindNews}>
          {isDiscussion ? "技术讨论" : "新闻"}
        </span>
        <div style={toolbarTitle}>{title}</div>
        {onClose && (
          <button type="button" onClick={onClose} style={closeBtn}>
            关闭
          </button>
        )}
      </header>

      <div className="item-reader-body" data-home-scroll="true">
        <div style={{ ...page, ...(isDiscussion ? pageDiscussion : null) }}>
          <div style={badgeRow}>
            <ImportanceBadge value={data.importance} />
            <InfoTypeBadge value={data.info_type} />
            {metaBits.map((bit) => (
              <span key={bit} style={metaChip}>{bit}</span>
            ))}
          </div>

          <h1 style={headline}>{title}</h1>
          {showSecondary && (
            <div style={kicker}>
              {isDiscussion ? `原始讨论主题：${secondaryTitle}` : secondaryTitle}
            </div>
          )}

          {!isDiscussion && (data.why_it_matters?.trim() || data.os_insight?.trim()) && (
            <div style={insightGrid}>
              {data.why_it_matters?.trim() && (
                <div style={insightCard}>
                  <div style={insightLabel}>推荐理由</div>
                  <div style={insightBody}>{data.why_it_matters}</div>
                </div>
              )}
              {data.os_insight?.trim() && (
                <div style={{ ...insightCard, borderColor: "#c5d6d3" }}>
                  <div style={{ ...insightLabel, color: "#3d6b62" }}>OS 启发</div>
                  <div style={insightBody}>{data.os_insight}</div>
                </div>
              )}
            </div>
          )}

          {data.summary && (
            <section style={isDiscussion ? conclusionCard : leadCard}>
              <div style={sectionLabel}>{isDiscussion ? "当前结论" : "摘要"}</div>
              <p style={leadText}>{data.summary}</p>
            </section>
          )}

          {highlights.length > 0 && (
            <section style={section}>
              <div style={sectionLabel}>{isDiscussion ? "观点与分歧" : "技术要点"}</div>
              <TechHighlightsList items={highlights} />
            </section>
          )}

          {isDiscussion && discussionQuery.isLoading && (
            <div style={quietNote}>正在载入讨论拓扑…</div>
          )}
          {isDiscussion && discussionQuery.isError && (
            <div style={errorNote}>讨论详情加载失败，摘要和要点仍可阅读。</div>
          )}
          {isDiscussion && discussionQuery.data && (
            <section style={section}>
              <DiscussionTopology discussion={discussionQuery.data} />
            </section>
          )}

          {(data.sub_tags.length > 0 || dedupedSourceLinks.length > 0) && (
            <section style={footerGrid}>
              {data.sub_tags.length > 0 && (
                <div style={{ minWidth: 0 }}>
                  <div style={sectionLabel}>技术热点</div>
                  <HotspotTags tags={data.sub_tags} />
                </div>
              )}
              {dedupedSourceLinks.length > 0 && (
                <div>
                  <div style={sectionLabel}>来源链接</div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                    {dedupedSourceLinks.map((s) => (
                      <SourceCta key={`${s.source_id}:${s.url}`} url={s.url} />
                    ))}
                  </div>
                </div>
              )}
            </section>
          )}
        </div>
      </div>
    </ReaderShell>
  );
}

export function ItemDetail({
  id,
  item,
  onClose,
  wide,
}: {
  id?: number;
  item?: ItemDetailRecord;
  onClose?: () => void;
  wide?: boolean;
}) {
  if (item) return <ItemDetailBody data={item} onClose={onClose} />;

  if (id === undefined) {
    return (
      <ReaderShell wide={wide}>
        <StatusState message="请选择一条内容查看详情" onClose={onClose} />
      </ReaderShell>
    );
  }

  const { data, isLoading, error } = useQuery({
    queryKey: ["item", id],
    queryFn: () => fetchItemDetail(id),
  });

  if (isLoading) {
    return (
      <ReaderShell wide={wide}>
        <StatusState message="加载中…" onClose={onClose} />
      </ReaderShell>
    );
  }

  if (error) {
    return (
      <ReaderShell wide={wide}>
        <StatusState
          message={`加载失败: ${error instanceof ApiError ? error.message : "未知错误"}`}
          tone="error"
          onClose={onClose}
        />
      </ReaderShell>
    );
  }

  if (!data) {
    return (
      <ReaderShell wide={wide}>
        <StatusState message="暂无数据" onClose={onClose} />
      </ReaderShell>
    );
  }

  return <ItemDetailBody data={data} onClose={onClose} />;
}

function StatusState({
  message,
  tone,
  onClose,
}: {
  message: string;
  tone?: "error";
  onClose?: () => void;
}) {
  return (
    <>
      <header style={toolbar}>
        <span style={kindNews}>详情</span>
        <div style={toolbarTitle}>{message}</div>
        {onClose && (
          <button type="button" onClick={onClose} style={closeBtn}>
            关闭
          </button>
        )}
      </header>
      <div style={{ ...statusBox, color: tone === "error" ? "#b42318" : "#667085" }}>{message}</div>
    </>
  );
}

const toolbar: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "12px 18px",
  borderBottom: "1px solid #eaecf0",
  background: "#fff",
  flexShrink: 0,
};
const toolbarTitle: CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontSize: 13,
  fontWeight: 700,
  color: "#344054",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};
const kindNews: CSSProperties = {
  flexShrink: 0,
  fontSize: 11,
  fontWeight: 800,
  letterSpacing: "0.06em",
  color: "#4a6785",
  background: "#eef1f5",
  borderRadius: 999,
  padding: "4px 9px",
};
const kindDiscussion: CSSProperties = {
  ...kindNews,
  color: "#3d6b62",
  background: "#ecf2f1",
};
const closeBtn: CSSProperties = {
  flexShrink: 0,
  border: "1px solid #d0d5dd",
  background: "#fff",
  borderRadius: 8,
  padding: "6px 12px",
  fontSize: 12,
  fontWeight: 700,
  color: "#344054",
  cursor: "pointer",
};
const page: CSSProperties = {
  padding: "22px 28px 32px",
  maxWidth: 720,
  margin: "0 auto",
};
const pageDiscussion: CSSProperties = {
  maxWidth: 980,
};
const badgeRow: CSSProperties = {
  display: "flex",
  gap: 8,
  marginBottom: 14,
  flexWrap: "wrap",
  alignItems: "center",
};
const metaChip: CSSProperties = {
  fontSize: 12,
  color: "#667085",
};
const headline: CSSProperties = {
  margin: "0 0 8px",
  fontSize: 26,
  lineHeight: 1.28,
  fontWeight: 800,
  color: "#101828",
  letterSpacing: "-0.02em",
};
const kicker: CSSProperties = {
  fontSize: 13,
  color: "#667085",
  marginBottom: 16,
  lineHeight: 1.5,
};
const insightGrid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
  gap: 10,
  margin: "4px 0 18px",
};
const insightCard: CSSProperties = {
  border: "1px solid #c8d2dc",
  borderRadius: 10,
  padding: "12px 14px",
  background: "#f8fafc",
};
const insightLabel: CSSProperties = {
  fontSize: 11,
  fontWeight: 800,
  letterSpacing: "0.06em",
  color: "#4a6785",
  marginBottom: 6,
};
const insightBody: CSSProperties = {
  fontSize: 13,
  lineHeight: 1.65,
  color: "#344054",
};
const section: CSSProperties = {
  marginBottom: 22,
};
const sectionLabel: CSSProperties = {
  fontSize: 11,
  fontWeight: 800,
  letterSpacing: "0.08em",
  color: "#98a2b3",
  marginBottom: 8,
};
const leadCard: CSSProperties = {
  margin: "0 0 22px",
  padding: "14px 16px",
  borderLeft: "3px solid #c8d2dc",
  background: "#f8fafc",
  borderRadius: "0 10px 10px 0",
};
const conclusionCard: CSSProperties = {
  ...leadCard,
  borderLeftColor: "#c5d6d3",
  background: "#f4faf8",
};
const leadText: CSSProperties = {
  margin: 0,
  fontSize: 15,
  lineHeight: 1.75,
  color: "#344054",
};
const footerGrid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
  gap: 20,
  marginTop: 8,
  paddingTop: 18,
  borderTop: "1px solid #eaecf0",
};
const statusBox: CSSProperties = {
  padding: 28,
};
const quietNote: CSSProperties = {
  color: "#667085",
  fontSize: 13,
  marginBottom: 16,
};
const errorNote: CSSProperties = {
  color: "#b42318",
  fontSize: 13,
  marginBottom: 16,
};
