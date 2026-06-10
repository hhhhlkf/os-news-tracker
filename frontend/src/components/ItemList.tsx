import { useQuery } from "@tanstack/react-query";
import { fetchItems, ApiError } from "../api/client";
import { ItemCard } from "./ItemCard";

interface Props {
  filters: Record<string, string>;
  onOpen: (id: number) => void;
}

export function ItemList({ filters, onOpen }: Props) {
  const params = Object.fromEntries(Object.entries(filters).filter(([, v]) => v));
  const { data, isLoading, error } = useQuery({
    queryKey: ["items", params],
    queryFn: () => fetchItems(params),
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
  if (data.total === 0) return <div>暂无结果</div>;
  return (
    <div style={{ flex: 1 }}>
      <div style={{ color: "#667085", marginBottom: 10 }}>共 {data.total} 条</div>
      {data.items.map((item) => (
        <ItemCard key={item.id} item={item} onClick={() => onOpen(item.id)} />
      ))}
    </div>
  );
}
