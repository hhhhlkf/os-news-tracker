import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ItemSummary } from "../types";
import { ImportanceBadge } from "./ImportanceBadge";
import { InfoTypeBadge } from "./InfoTypeBadge";
import { generateItemReason } from "../api/client";

function RecommendationReason({ item }: { item: ItemSummary }) {
  const existing = item.why_it_matters?.trim() || "";
  const { data, isLoading } = useQuery({
    queryKey: ["item-reason", item.id],
    queryFn: () => generateItemReason(item.id),
    enabled: existing.length === 0,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const reason = existing || data?.reason?.trim() || "";

  let body: ReactNode;
  if (reason) {
    body = reason;
  } else if (isLoading) {
    body = <span style={{ color: "#98a2b3" }}>正在生成推荐理由…</span>;
  } else {
    body = <span style={{ color: "#98a2b3" }}>暂无推荐理由</span>;
  }

  return (
    <div
      style={{
        marginTop: 10,
        paddingTop: 10,
        borderTop: "1px dashed #eaecf0",
        fontSize: 13,
        lineHeight: 1.6,
        color: "#475467",
      }}
    >
      <span style={{ color: "#175cd3", fontWeight: 600 }}>推荐理由：</span>
      {body}
    </div>
  );
}

export function ItemCard({ item, onClick, sortBy }: {
  item: ItemSummary;
  onClick: () => void;
  sortBy?: "published_at" | "fetched_at";
}) {
  const isFetchedAt = sortBy === "fetched_at";
  return (
    <button onClick={onClick} style={{
      display: "block", width: "100%", textAlign: "left",
      border: "1px solid #eaecf0", borderRadius: 10, padding: 16,
      marginBottom: 10, background: "#fff", cursor: "pointer",
    }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
        <ImportanceBadge value={item.importance} />
        <InfoTypeBadge value={item.info_type} />
        {item.main_category && (
          <span style={{ fontSize: 12, color: "#667085" }}>{item.main_category}</span>
        )}
        {isFetchedAt
          ? item.fetched_at && (
              <span style={{ fontSize: 12, color: "#98a2b3", marginLeft: "auto" }}>
                入库 {item.fetched_at.slice(0, 10)}
              </span>
            )
          : item.published_at && (
              <span style={{ fontSize: 12, color: "#98a2b3", marginLeft: "auto" }}>
                {item.published_at.slice(0, 10)}
              </span>
            )
        }
      </div>
      <div style={{ fontWeight: 600 }}>{item.title_tldr ?? item.title}</div>
      <RecommendationReason item={item} />
    </button>
  );
}
