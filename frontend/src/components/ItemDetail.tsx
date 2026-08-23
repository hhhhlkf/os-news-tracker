import { useQuery } from "@tanstack/react-query";
import { fetchItemDetail, ApiError } from "../api/client";
import type { ItemDetail as ItemDetailRecord } from "../types";
import { fetchDiscussionDetail } from "../discussions/api";
import { DiscussionTopology } from "./DiscussionTopology";
import { ImportanceBadge } from "./ImportanceBadge";
import { InfoTypeBadge } from "./InfoTypeBadge";
import { HotspotTags, SourceCta, TechHighlightsList } from "./ItemMetaBlocks";

function ItemDetailBody({ data }: { data: ItemDetailRecord }) {
  const dedupedSourceLinks = Array.from(
    new Map(data.source_links.map((s) => [`${s.source_id}:${s.url}`, s])).values(),
  );
  const isDiscussion = data.item_kind === "discussion";
  const discussionQuery = useQuery({ queryKey: ["discussion-detail", data.id], queryFn: () => fetchDiscussionDetail(data.id), enabled: isDiscussion, retry: false });
  const secondaryTitle = isDiscussion ? data.original_title : data.title;
  return (
    <div style={{ padding: 20 }}>
      <div style={{ display: "flex", gap: 8, marginBottom: 8, flexWrap: "wrap" }}>
        <ImportanceBadge value={data.importance} />
        <InfoTypeBadge value={data.info_type} />
        <span style={{ fontSize: 12, color: "#667085" }}>{data.main_category}</span>
      </div>

      {data.title_tldr ? (
        <>
          <h2 style={{ margin: "4px 0 4px" }}>{data.title_tldr}</h2>
          {secondaryTitle && secondaryTitle !== data.title_tldr && (
            <div style={{ fontSize: 13, color: "#667085", marginBottom: 12 }}>
              {isDiscussion ? `原始讨论主题：${secondaryTitle}` : secondaryTitle}
            </div>
          )}
        </>
      ) : (
        <h2 style={{ margin: "4px 0 12px" }}>{data.title}</h2>
      )}

      {data.summary && (
        <section style={{ marginBottom: 16 }}>
          <h4 style={{ color: "#475467" }}>摘要</h4>
          <p>{data.summary}</p>
        </section>
      )}

      {data.key_points.filter((kp) => !kp.startsWith("__type:")).length > 0 && (
        <section style={{ marginBottom: 16 }}>
          <h4 style={{ color: "#475467" }}>技术要点</h4>
          <TechHighlightsList items={data.key_points} />
        </section>
      )}

      {isDiscussion && discussionQuery.data && <DiscussionTopology discussion={discussionQuery.data} />}

      {(data.sub_tags.length > 0 || dedupedSourceLinks.length > 0) && (
        <section
          style={{
            display: "flex",
            gap: 24,
            flexWrap: "wrap",
            alignItems: "flex-start",
          }}
        >
          {data.sub_tags.length > 0 && (
            <div style={{ flex: "1 1 240px", minWidth: 0 }}>
              <h4 style={{ color: "#475467", margin: "0 0 8px" }}>技术热点</h4>
              <HotspotTags tags={data.sub_tags} />
            </div>
          )}

          {dedupedSourceLinks.length > 0 && (
            <div style={{ flexShrink: 0 }}>
              <h4 style={{ color: "#475467", margin: "0 0 8px" }}>来源链接</h4>
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
  );
}

export function ItemDetail({ id, item }: { id?: number; item?: ItemDetailRecord }) {
  if (item) return <ItemDetailBody data={item} />;

  if (id === undefined) {
    return <div style={{ padding: 16, color: "#667085" }}>请选择一条内容查看详情</div>;
  }

  const { data, isLoading, error } = useQuery({
    queryKey: ["item", id],
    queryFn: () => fetchItemDetail(id),
  });

  if (isLoading) return <div>加载中…</div>;

  if (error) {
    return (
      <div style={{ color: "#b42318", padding: 16 }}>
        加载失败: {error instanceof ApiError ? `${error.message}` : "未知错误"}
      </div>
    );
  }

  if (!data) return <div>暂无数据</div>;

  return <ItemDetailBody data={data} />;
}
