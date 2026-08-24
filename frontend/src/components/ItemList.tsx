import { useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import type { ItemSummary } from "../types";
import { ItemCard } from "./ItemCard";

type PageSlot = number | "ellipsis";

function buildPageNumbers(current: number, total: number): PageSlot[] {
  if (total <= 7) {
    return Array.from({ length: total }, (_, i) => i + 1);
  }

  const slots: PageSlot[] = [1];

  if (current > 3) {
    slots.push("ellipsis");
  }

  const start = Math.max(2, current - 1);
  const end = Math.min(total - 1, current + 1);
  for (let p = start; p <= end; p++) {
    slots.push(p);
  }

  if (current < total - 2) {
    slots.push("ellipsis");
  }

  slots.push(total);
  return slots;
}

interface Props {
  items: ItemSummary[];
  total: number;
  page: number;
  pageSize: number;
  isLoading?: boolean;
  /** True while the next page is loading but previous items may still be shown. */
  isFetching?: boolean;
  emptyMessage?: string;
  sortBy?: "published_at" | "fetched_at" | "last_activity_at";
  openId?: number | null;
  onOpen: (id: number) => void;
  onPageChange: (page: number) => void;
}

export function ItemList({
  items,
  total,
  page,
  pageSize,
  isLoading,
  isFetching,
  emptyMessage,
  sortBy,
  openId,
  onOpen,
  onPageChange,
}: Props) {
  const directionRef = useRef<1 | -1>(1);
  const [enterDir, setEnterDir] = useState<1 | -1>(1);
  const [enterKey, setEnterKey] = useState(`${page}:init`);
  const prevSettledRef = useRef({ page, headId: items[0]?.id ?? null, len: items.length });

  const contentKey = `${page}:${items[0]?.id ?? "empty"}:${items.length}`;
  const busy = Boolean(isFetching);

  useLayoutEffect(() => {
    if (busy) return;
    const prev = prevSettledRef.current;
    const headId = items[0]?.id ?? null;
    const changed = prev.page !== page || prev.headId !== headId || prev.len !== items.length;
    if (!changed) return;
    // Page changes may come from outside (edge arrows), so derive the enter
    // direction from the page delta instead of relying on changePage alone.
    const dir = prev.page !== page ? (page > prev.page ? 1 : -1) : directionRef.current;
    directionRef.current = dir;
    setEnterDir(dir);
    setEnterKey(contentKey);
    prevSettledRef.current = { page, headId, len: items.length };
  }, [busy, contentKey, items, page]);

  function changePage(next: number) {
    if (next === page) return;
    directionRef.current = next > page ? 1 : -1;
    onPageChange(next);
  }

  if (isLoading && items.length === 0) {
    return <div style={{ color: "#667085", padding: 20 }}>正在加载条目…</div>;
  }

  if (total === 0 && !busy) {
    return (
      <div
        style={{
          border: "1px dashed #d0d5dd",
          borderRadius: 8,
          padding: 24,
          background: "#fff",
          color: "#667085",
        }}
      >
        {emptyMessage ?? "暂无结果"}
      </div>
    );
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const pageNumbers = buildPageNumbers(page, totalPages);
  const enterClass = enterDir >= 0 ? "news-page-enter-next" : "news-page-enter-prev";

  return (
    <div style={{ flex: 1 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 12,
          marginBottom: 10,
          color: "#667085",
          minHeight: 20,
        }}
      >
        <div>共 {total} 条</div>
        <div
          style={{
            fontSize: 12,
            color: "#98a2b3",
            opacity: busy ? 1 : 0,
            transition: "opacity 160ms ease",
          }}
        >
          加载中…
        </div>
      </div>

      <div
        className="news-page-list"
        style={{
          opacity: busy ? 0.42 : 1,
          transform: busy ? `translate3d(${directionRef.current * -10}px, 0, 0)` : "translate3d(0, 0, 0)",
          filter: busy ? "saturate(0.9)" : "none",
          transition: "opacity 200ms ease, transform 240ms cubic-bezier(0.22, 1, 0.36, 1), filter 200ms ease",
          pointerEvents: busy ? "none" : "auto",
          willChange: "opacity, transform",
        }}
      >
        <div
          key={enterKey}
          className={enterClass}
          style={{
            animation: busy
              ? "none"
              : `${enterDir >= 0 ? "news-page-enter-next" : "news-page-enter-prev"} 320ms cubic-bezier(0.22, 1, 0.36, 1)`,
          }}
        >
          {items.map((item) => (
            <ItemCard
              key={item.id}
              item={item}
              onClick={() => onOpen(item.id)}
              selected={item.id === openId}
              sortBy={sortBy}
            />
          ))}
        </div>
      </div>

      <div
        style={{
          display: "flex",
          justifyContent: "center",
          alignItems: "center",
          gap: 8,
          marginTop: 16,
          padding: "12px 0",
          borderTop: "1px solid #eaecf0",
          flexWrap: "wrap",
        }}
      >
        <button
          type="button"
          onClick={() => changePage(page - 1)}
          disabled={page <= 1}
          style={navButtonStyle(page <= 1)}
        >
          上一页
        </button>
        {pageNumbers.map((item, idx) =>
          item === "ellipsis" ? (
            <span key={`ellipsis-${idx}`} style={{ padding: "0 2px", color: "#98a2b3", fontSize: 13 }}>
              …
            </span>
          ) : (
            <button
              type="button"
              key={item}
              onClick={() => changePage(item)}
              style={pageButtonStyle(item === page)}
            >
              {item}
            </button>
          ),
        )}
        <button
          type="button"
          onClick={() => changePage(page + 1)}
          disabled={page >= totalPages}
          style={navButtonStyle(page >= totalPages)}
        >
          下一页
        </button>
      </div>
    </div>
  );
}

function navButtonStyle(disabled: boolean): CSSProperties {
  return {
    border: "1px solid #d0d5dd",
    borderRadius: 8,
    padding: "8px 14px",
    background: "#fff",
    color: disabled ? "#98a2b3" : "#344054",
    fontSize: 13,
    cursor: disabled ? "default" : "pointer",
    transition: "background 160ms ease, border-color 160ms ease, color 160ms ease, transform 160ms ease",
    opacity: disabled ? 0.75 : 1,
  };
}

function pageButtonStyle(active: boolean): CSSProperties {
  return {
    minWidth: 36,
    height: 36,
    border: active ? "1px solid #175cd3" : "1px solid #d0d5dd",
    borderRadius: 8,
    background: active ? "#175cd3" : "#fff",
    color: active ? "#fff" : "#344054",
    fontSize: 13,
    fontWeight: active ? 700 : 400,
    cursor: "pointer",
    transition: "background 180ms ease, border-color 180ms ease, color 180ms ease, transform 160ms ease",
    transform: active ? "translateY(-1px)" : "none",
    boxShadow: active ? "0 4px 10px rgba(23, 92, 211, 0.18)" : "none",
  };
}
