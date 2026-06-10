import { useState } from "react";
import { FacetSidebar } from "../components/FacetSidebar";
import { ItemList } from "../components/ItemList";
import { ItemDetail } from "../components/ItemDetail";

export function HomePage() {
  const [filters, setFilters] = useState<Record<string, string>>({ q: "" });
  const [openId, setOpenId] = useState<number | null>(null);

  const setFilter = (key: string, value: string) =>
    setFilters((f) => ({ ...f, [key]: value }));

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: 24 }}>
      <h1>技术新闻追踪</h1>
      <input
        placeholder="搜索标题/摘要…"
        value={filters.q ?? ""}
        onChange={(e) => setFilter("q", e.target.value)}
        style={{ width: "100%", padding: 10, marginBottom: 16,
                 border: "1px solid #d0d5dd", borderRadius: 8 }}
      />
      <div style={{ display: "flex" }}>
        <FacetSidebar selected={filters} onSelect={setFilter} />
        <ItemList filters={filters} onOpen={setOpenId} />
      </div>
      {openId !== null && (
        <div onClick={() => setOpenId(null)} style={{
          position: "fixed", inset: 0, background: "rgba(0,0,0,0.35)",
          display: "flex", justifyContent: "flex-end",
        }}>
          <div onClick={(e) => e.stopPropagation()} style={{
            width: 560, maxWidth: "90vw", background: "#fff",
            height: "100%", overflowY: "auto",
          }}>
            <ItemDetail id={openId} />
          </div>
        </div>
      )}
    </div>
  );
}
