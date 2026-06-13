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

function ItemDetailBody({ data }: { data: ItemDetailRecord }) {
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

      {data.sub_tags.length > 0 && (
        <section style={{ marginBottom: 16 }}>
          <h4 style={{ color: "#475467" }}>技术热点</h4>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {data.sub_tags.map((tag, i) => (
              <span key={i} style={{
                background: "#f2f4f7", borderRadius: 6, padding: "2px 8px", fontSize: 12,
              }}>{tag}</span>
            ))}
          </div>
        </section>
      )}

      {data.source_links.length > 0 && (
        <section>
          <h4 style={{ color: "#475467" }}>来源链接</h4>
          <ul>
            {Array.from(
              new Map(data.source_links.map((s) => [`${s.source_id}:${s.url}`, s])).values(),
            ).map((s) => (
              <li key={`${s.source_id}:${s.url}`}>
                <a href={s.url} target="_blank" rel="noreferrer">{s.url}</a>
              </li>
            ))}
          </ul>
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
