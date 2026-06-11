import type { Facets } from "../types";

interface Props {
  facets: Facets;
  isLoading?: boolean;
  selected: Record<string, string>;
  onSelect: (key: string, value: string) => void;
}

export function FacetSidebar({ facets, isLoading, selected, onSelect }: Props) {
  if (isLoading) {
    return (
      <aside style={{ width: 260, paddingRight: 20 }}>
        <div style={{ color: "#667085", fontSize: 14 }}>正在加载筛选项…</div>
      </aside>
    );
  }

  const groups: [keyof Facets, string][] = [
    ["main_category", "主分类"],
    ["info_type", "信息类型"],
    ["importance", "重要度"],
  ];

  return (
    <aside style={{ width: 260, paddingRight: 20, flexShrink: 0 }}>
      {groups.map(([key, label]) => (
        <section
          key={key}
          style={{
            marginBottom: 16,
            border: "1px solid #d0d5dd",
            borderRadius: 8,
            padding: 14,
            background: "#fff",
          }}
        >
          <div style={{ fontWeight: 600, marginBottom: 10, color: "#101828" }}>{label}</div>
          {facets[key].map((f: { value: string; count: number }) => (
            <div key={f.value}
                 onClick={() => onSelect(key, selected[key] === f.value ? "" : f.value)}
                 style={{
                   cursor: "pointer",
                   padding: "8px 10px",
                   borderRadius: 6,
                   background: selected[key] === f.value ? "#eff8ff" : "transparent",
                   color: selected[key] === f.value ? "#175cd3" : "#344054",
                   fontSize: 13,
                   display: "flex",
                   justifyContent: "space-between",
                 }}>
              <span>{f.value}</span>
              <span style={{ color: "#98a2b3" }}>{f.count}</span>
            </div>
          ))}
        </section>
      ))}
    </aside>
  );
}
