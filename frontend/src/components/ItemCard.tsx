import type { ItemSummary } from "../types";
import { ImportanceBadge } from "./ImportanceBadge";
import { InfoTypeBadge } from "./InfoTypeBadge";

export function ItemCard({ item, onClick }: { item: ItemSummary; onClick: () => void }) {
  return (
    <button onClick={onClick} style={{
      display: "block", width: "100%", textAlign: "left",
      border: "1px solid #eaecf0", borderRadius: 10, padding: 14,
      marginBottom: 10, background: "#fff", cursor: "pointer",
    }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}>
        <ImportanceBadge value={item.importance} />
        <InfoTypeBadge value={item.info_type} />
        {item.main_category && (
          <span style={{ fontSize: 12, color: "#667085" }}>{item.main_category}</span>
        )}
        {item.published_at && (
          <span style={{ fontSize: 12, color: "#98a2b3", marginLeft: "auto" }}>
            {item.published_at.slice(0, 10)}
          </span>
        )}
      </div>
      <div style={{ fontWeight: 600 }}>{item.title_tldr ?? item.title}</div>
    </button>
  );
}
