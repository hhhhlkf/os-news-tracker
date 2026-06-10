import { useQuery } from "@tanstack/react-query";
import { fetchFacets, ApiError } from "../api/client";
import type { Facets } from "../types";

interface Props {
  selected: Record<string, string>;
  onSelect: (key: string, value: string) => void;
}

export function FacetSidebar({ selected, onSelect }: Props) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["facets"],
    queryFn: fetchFacets,
  });

  if (isLoading) return <div>加载中…</div>;

  if (error) {
    return (
      <aside style={{ width: 220, paddingRight: 16 }}>
        <div style={{ color: "#b42318", padding: 8 }}>
          加载失败: {error instanceof ApiError ? `${error.message}` : "未知错误"}
        </div>
      </aside>
    );
  }

  if (!data) return null;

  const groups: [keyof Facets, string][] = [
    ["main_category", "主分类"],
    ["info_type", "信息类型"],
    ["importance", "重要度"],
  ];

  return (
    <aside style={{ width: 220, paddingRight: 16 }}>
      {groups.map(([key, label]) => (
        <div key={key} style={{ marginBottom: 16 }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>{label}</div>
          {data[key].map((f: { value: string; count: number }) => (
            <div key={f.value}
                 onClick={() => onSelect(key, selected[key] === f.value ? "" : f.value)}
                 style={{
                   cursor: "pointer", padding: "4px 6px", borderRadius: 6,
                   background: selected[key] === f.value ? "#eff8ff" : "transparent",
                   fontSize: 13,
                 }}>
              {f.value} <span style={{ color: "#98a2b3" }}>({f.count})</span>
            </div>
          ))}
        </div>
      ))}
    </aside>
  );
}
