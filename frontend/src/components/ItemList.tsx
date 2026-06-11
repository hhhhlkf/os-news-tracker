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
  emptyMessage?: string;
  sortBy?: "published_at" | "fetched_at";
  onOpen: (id: number) => void;
  onPageChange: (page: number) => void;
}

export function ItemList({ items, total, page, pageSize, isLoading, emptyMessage, sortBy, onOpen, onPageChange }: Props) {
  if (isLoading) {
    return <div style={{ color: "#667085", padding: 20 }}>正在加载条目…</div>;
  }

  if (total === 0) {
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

  return (
    <div style={{ flex: 1 }}>
      <div style={{ color: "#667085", marginBottom: 10 }}>共 {total} 条</div>
      {items.map((item) => (
        <ItemCard key={item.id} item={item} onClick={() => onOpen(item.id)} sortBy={sortBy} />
      ))}
      <div style={{
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
        gap: 8,
        marginTop: 16,
        padding: "12px 0",
        borderTop: "1px solid #eaecf0",
        flexWrap: "wrap",
      }}>
        <button
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1}
          style={{
            border: "1px solid #d0d5dd",
            borderRadius: 8,
            padding: "8px 14px",
            background: "#fff",
            color: page <= 1 ? "#98a2b3" : "#344054",
            fontSize: 13,
            cursor: page <= 1 ? "default" : "pointer",
          }}
        >
          上一页
        </button>
        {pageNumbers.map((item, idx) =>
          item === "ellipsis" ? (
            <span key={`ellipsis-${idx}`} style={{ padding: "0 2px", color: "#98a2b3", fontSize: 13 }}>…</span>
          ) : (
            <button
              key={item}
              onClick={() => onPageChange(item)}
              style={{
                minWidth: 36,
                height: 36,
                border: item === page ? "1px solid #175cd3" : "1px solid #d0d5dd",
                borderRadius: 8,
                background: item === page ? "#175cd3" : "#fff",
                color: item === page ? "#fff" : "#344054",
                fontSize: 13,
                fontWeight: item === page ? 700 : 400,
                cursor: "pointer",
              }}
            >
              {item}
            </button>
          ),
        )}
        <button
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages}
          style={{
            border: "1px solid #d0d5dd",
            borderRadius: 8,
            padding: "8px 14px",
            background: "#fff",
            color: page >= totalPages ? "#98a2b3" : "#344054",
            fontSize: 13,
            cursor: page >= totalPages ? "default" : "pointer",
          }}
        >
          下一页
        </button>
      </div>
    </div>
  );
}
