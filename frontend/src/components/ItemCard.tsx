import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ItemSummary } from "../types";
import { ImportanceBadge } from "./ImportanceBadge";
import { InfoTypeBadge } from "./InfoTypeBadge";
import { generateItemOsInsight, generateItemReason } from "../api/client";
import { claimCardGenerationSlot, runQueuedItemLlmCall } from "../llmGenerationQueue";

/** Compact meta lines under the title — shared size so 推荐理由 / OS启发 match. */
const metaLine: CSSProperties = {
  fontSize: 11.5,
  lineHeight: 1.42,
  color: "#475467",
};

const discussionMetaLine: CSSProperties = {
  ...metaLine,
  minHeight: 40,
  display: "flex",
  alignItems: "center",
  gap: 4,
  fontSize: 12.5,
  lineHeight: 1.52,
  padding: "2px 0",
  boxSizing: "border-box",
};

function ItemLlmFields({ item }: { item: ItemSummary }) {
  if (item.item_kind === "discussion") {
    return (
      <div style={discussionMetaLine}>
        <span style={{ color: "#3d6b62", fontWeight: 600 }}>讨论摘要：</span>
        <span style={{ flex: 1, minWidth: 0 }}>{item.why_it_matters?.trim() || "打开详情查看当前结论、分歧与讨论进展。"}</span>
      </div>
    );
  }
  const needReason = !(item.why_it_matters?.trim());
  const needInsight = !(item.os_insight?.trim());
  const needsGeneration = needReason || needInsight;

  const [gateOpen, setGateOpen] = useState(false);
  const releaseRef = useRef<(() => void) | null>(null);
  const finishedRef = useRef(false);

  useEffect(() => {
    if (!needsGeneration || finishedRef.current) {
      setGateOpen(false);
      return;
    }
    let cancelled = false;
    void (async () => {
      const release = await claimCardGenerationSlot();
      if (cancelled || finishedRef.current) {
        release();
        return;
      }
      releaseRef.current = release;
      setGateOpen(true);
    })();
    return () => {
      cancelled = true;
      releaseRef.current?.();
      releaseRef.current = null;
      setGateOpen(false);
    };
  }, [item.id, needsGeneration]);

  const reasonQuery = useQuery({
    queryKey: ["item-reason", item.id],
    queryFn: () => runQueuedItemLlmCall(() => generateItemReason(item.id)),
    enabled: gateOpen && needReason,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const insightQuery = useQuery({
    queryKey: ["item-os-insight", item.id],
    queryFn: () => runQueuedItemLlmCall(() => generateItemOsInsight(item.id)),
    enabled: gateOpen && needInsight,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  });

  const reasonDone = !needReason || reasonQuery.isFetched || reasonQuery.isError;
  const insightDone = !needInsight || insightQuery.isFetched || insightQuery.isError;
  const allDone = reasonDone && insightDone;

  useEffect(() => {
    if (!gateOpen || !allDone || finishedRef.current) return;
    finishedRef.current = true;
    releaseRef.current?.();
    releaseRef.current = null;
  }, [gateOpen, allDone]);

  const reason = item.why_it_matters?.trim() || reasonQuery.data?.reason?.trim() || "";
  const insight = item.os_insight?.trim() || insightQuery.data?.os_insight?.trim() || "";
  const reasonWaiting = needReason && !reason && !reasonQuery.isFetched && !reasonQuery.isError;
  const insightWaiting = needInsight && !insight && !insightQuery.isFetched && !insightQuery.isError;

  let reasonBody: ReactNode;
  if (reason) {
    reasonBody = reason;
  } else if (reasonWaiting) {
    reasonBody = (
      <span style={{ color: "#98a2b3" }}>
        {gateOpen && reasonQuery.isFetching ? "正在生成推荐理由…" : "排队生成推荐理由…"}
      </span>
    );
  } else {
    reasonBody = <span style={{ color: "#98a2b3" }}>暂无推荐理由</span>;
  }

  let insightBody: ReactNode;
  if (insight) {
    insightBody = insight;
  } else if (insightWaiting) {
    insightBody = (
      <span style={{ color: "#98a2b3" }}>
        {gateOpen && insightQuery.isFetching ? "正在生成 OS 启发…" : "排队生成 OS 启发…"}
      </span>
    );
  } else {
    insightBody = <span style={{ color: "#98a2b3" }}>暂无 OS 启发</span>;
  }

  return (
    <>
      <div style={metaLine}>
        <span style={{ color: "#175cd3", fontWeight: 600 }}>推荐理由：</span>
        {reasonBody}
      </div>
      <div style={{ ...metaLine, marginTop: 3 }}>
        <span style={{ color: "#0e7490", fontWeight: 600 }}>OS启发：</span>
        {insightBody}
      </div>
    </>
  );
}

export function ItemCard({ item, onClick, selected = false, sortBy }: {
  item: ItemSummary;
  onClick: () => void;
  selected?: boolean;
  sortBy?: "published_at" | "fetched_at" | "last_activity_at";
}) {
  const isFetchedAt = sortBy === "fetched_at";
  const isDiscussion = item.item_kind === "discussion";
  return (
    <button className={selected ? "news-item-card news-item-card--selected" : "news-item-card"} onClick={onClick} style={{
      display: "block", width: "100%", textAlign: "left",
      border: "1px solid #eaecf0", borderRadius: 10, padding: "13px 16px",
      marginBottom: 10, background: "#fff", cursor: "pointer",
    }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 5 }}>
        <ImportanceBadge value={item.importance} />
        <InfoTypeBadge value={item.info_type} />
        {isDiscussion && (
          <span style={{ fontSize: 11, borderRadius: 999, padding: "2px 7px", background: "#ecf2f1", color: "#3d6b62", fontWeight: 700 }}>技术讨论</span>
        )}
        {item.main_category && (
          <span style={{ fontSize: 12, color: "#667085" }}>{item.main_category}</span>
        )}
        {isFetchedAt
          ? item.fetched_at && (
              <span style={{ fontSize: 12, color: "#98a2b3", marginLeft: "auto" }}>
                入库 {item.fetched_at.slice(0, 10)}
              </span>
            )
          : (isDiscussion ? item.last_activity_at : item.published_at) && (
              <span style={{ fontSize: 12, color: "#98a2b3", marginLeft: "auto" }}>
                {isDiscussion ? `活动 ${(item.last_activity_at ?? "").slice(0, 10)}` : item.published_at?.slice(0, 10)}
              </span>
            )
        }
      </div>
      <div style={{ fontWeight: 600, lineHeight: 1.38 }}>{item.title_tldr ?? item.title}</div>
      <div
        style={{
          marginTop: 6,
          paddingTop: 6,
          borderTop: "1px dashed #eaecf0",
        }}
      >
        <ItemLlmFields item={item} />
      </div>
    </button>
  );
}
