import { useQuery } from "@tanstack/react-query";
import { fetchItemDetail, ApiError } from "../api/client";
import type { ItemDetail as ItemDetailRecord } from "../types";
import { ImportanceBadge } from "./ImportanceBadge";
import { InfoTypeBadge } from "./InfoTypeBadge";

function parseTechHighlight(text: string): { keyword: string; detail: string } | null {
  const match = text.match(/^\[(.+?)\]\s*(.*)/);
  if (match) {
    return { keyword: match[1], detail: match[2] };
  }
  return null;
}

function domainFromUrl(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function SourceCta({ url }: { url: string }) {
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        padding: "8px 12px",
        border: "1px solid #d0d5dd",
        borderRadius: 8,
        background: "#fff",
        color: "#344054",
        fontSize: 13,
        textDecoration: "none",
        lineHeight: 1,
        width: "fit-content",
      }}
    >
      <span>阅读原文</span>
      <svg
        xmlns="http://www.w3.org/2000/svg"
        width={16}
        height={16}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
        <polyline points="15 3 21 3 21 9" />
        <line x1="10" y1="14" x2="21" y2="3" />
      </svg>
      <span style={{ color: "#98a2b3", fontSize: 12 }}>{domainFromUrl(url)}</span>
    </a>
  );
}

function ItemDetailBody({ data }: { data: ItemDetailRecord }) {
  const dedupedSourceLinks = Array.from(
    new Map(data.source_links.map((s) => [`${s.source_id}:${s.url}`, s])).values(),
  );
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
          <div style={{ fontSize: 13, color: "#667085", marginBottom: 12 }}>{data.title}</div>
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

      {data.key_points.length > 0 && (
        <section style={{ marginBottom: 16 }}>
          <h4 style={{ color: "#475467" }}>技术要点</h4>
          <div>
            {data.key_points.map((kp, i) => {
              const parsed = parseTechHighlight(kp);
              return (
                <div
                  key={i}
                  style={{
                    border: "1px solid #eaecf0",
                    borderRadius: 8,
                    padding: "8px 12px",
                    marginBottom: 6,
                    background: "#fafafa",
                  }}
                >
                  {parsed ? (
                    <>
                      <strong style={{ color: "#175cd3" }}>[{parsed.keyword}]</strong>{" "}
                      {parsed.detail}
                    </>
                  ) : (
                    kp
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

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
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                {data.sub_tags.map((tag, i) => (
                  <span key={i} style={{
                    background: "#f2f4f7", borderRadius: 6, padding: "2px 8px", fontSize: 12,
                  }}>{tag}</span>
                ))}
              </div>
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
