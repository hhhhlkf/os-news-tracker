// frontend/src/components/CrawlMethodDetail.tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { CSSProperties } from "react";
import { ApiError, deleteDiscoveryMethod, getDiscoveryMethod, patchDiscoveryMethod } from "../api/client";

export function CrawlMethodDetail({ methodId, onClose, allowManage = false }: { methodId: number; onClose: () => void; allowManage?: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["discovery-method", methodId], queryFn: () => getDiscoveryMethod(methodId) });
  const patchMut = useMutation({
    mutationFn: (status: "active" | "disabled") => patchDiscoveryMethod(methodId, status),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ["discovery-methods"] });
      await qc.invalidateQueries({ queryKey: ["discovery-method", methodId] });
    },
  });
  const delMut = useMutation({
    mutationFn: () => deleteDiscoveryMethod(methodId),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["discovery-methods"] }); onClose(); },
    onError: (e) => alert(e instanceof ApiError ? e.message : "删除失败"),
  });

  return (
    <div onClick={onClose} style={{ position: "fixed", inset: 0, background: "rgba(15,23,42,0.42)", display: "flex", justifyContent: "flex-end" }}>
      <div onClick={(e) => e.stopPropagation()} style={{ width: 560, maxWidth: "92vw", background: "#fff", height: "100%", overflowY: "auto", boxShadow: "-24px 0 48px rgba(16,24,40,0.16)", padding: 20 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <div style={{ fontSize: 18, fontWeight: 800 }}>{q.data?.domain ?? "加载中…"}</div>
          <a style={{ cursor: "pointer", color: "#667085" }} onClick={onClose}>关闭</a>
        </div>
        <div style={{ fontSize: 12, color: "#667085", wordBreak: "break-all", marginBottom: 12 }}>{q.data?.entry_url}</div>
        {q.isLoading && (
          <div style={infoBox}>
            加载爬取方式详情中...
          </div>
        )}
        {q.isError && (
          <div style={{ ...infoBox, border: "1px solid #fecdca", background: "#fef3f2", color: "#b42318" }}>
            加载爬取方式详情失败，请稍后重试。
          </div>
        )}
        {q.data && (
          <>
            {allowManage && (
              <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
                <button type="button" style={btnPrimary} disabled={patchMut.isPending}
                  onClick={() => patchMut.mutate(q.data!.status === "active" ? "disabled" : "active")}>
                  {q.data.status === "active" ? "禁用" : "启用"}
                </button>
                <button type="button" style={btnDanger} disabled={delMut.isPending}
                  onClick={() => { if (confirm("删除该爬取方式？")) delMut.mutate(); }}>删除</button>
              </div>
            )}
            <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>DSL Recipe</div>
            <pre style={{ background: "#0b1220", color: "#d0d5dd", borderRadius: 8, padding: 12, fontSize: 12, overflow: "auto", fontFamily: "JetBrains Mono, monospace" }}>
{JSON.stringify(q.data.dsl_recipe, null, 2)}
            </pre>
          </>
        )}
      </div>
    </div>
  );
}

const btnPrimary: CSSProperties = { border: "none", borderRadius: 999, padding: "8px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnDanger: CSSProperties = { border: "1px solid #fecdca", borderRadius: 999, padding: "8px 16px", background: "#fff", color: "#b42318", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const infoBox: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 16, color: "#667085", fontSize: 13 };
