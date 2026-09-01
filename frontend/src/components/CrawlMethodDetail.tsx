// frontend/src/components/CrawlMethodDetail.tsx
import { useEffect, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, deleteDiscoveryMethod, getDiscoveryMethod, getDiscoveryMethodSource, patchDiscoveryMethod } from "../api/client";
import { CrawlRecipeFlow } from "./CrawlRecipeFlow";

export function CrawlMethodDetail({ methodId, onClose, allowManage = false }: { methodId: number; onClose: () => void; allowManage?: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["discovery-method", methodId], queryFn: () => getDiscoveryMethod(methodId) });
  const isPythonConnector = q.data?.dsl_recipe.recipe_type === "python_plugin";
  const sourceQuery = useQuery({
    queryKey: ["discovery-method-source", methodId],
    queryFn: () => getDiscoveryMethodSource(methodId),
    enabled: allowManage && isPythonConnector,
  });
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

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const title = q.data?.domain ?? "加载中…";

  return (
    <div className="item-reader-overlay" onClick={onClose}>
      <div
        className="item-reader-sheet"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <header style={toolbar}>
          <span style={kindPill}>爬取方式</span>
          <div style={toolbarTitle}>{title}</div>
          <button type="button" onClick={onClose} style={closeBtn}>关闭</button>
        </header>
        <div className="item-reader-body" data-home-scroll="true">
          <div style={page}>
            <div style={entryUrl}>{q.data?.entry_url}</div>
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
                <CrawlRecipeFlow recipe={q.data.dsl_recipe} executionSteps={q.data.execution_steps} />
                {allowManage && isPythonConnector && (
                  <details open style={{ marginBottom: 16 }}>
                    <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 700, color: "#344054", marginBottom: 8 }}>Python Connector 源码</summary>
                    {sourceQuery.isLoading && <div style={infoBox}>加载并校验 crawler.py...</div>}
                    {sourceQuery.isError && (
                      <div style={{ ...infoBox, borderColor: "#fecdca", color: "#b42318" }}>源码校验或加载失败。</div>
                    )}
                    {sourceQuery.data && (
                      <pre className="scrollbar-on-dark" style={sourceCode}>
                        <code>{sourceQuery.data.source}</code>
                      </pre>
                    )}
                  </details>
                )}
                <details>
                  <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 700, color: "#344054", marginBottom: 8 }}>DSL Recipe（原始配置）</summary>
                  <pre
                    className="scrollbar-on-dark"
                    style={{ marginTop: 10, background: "#0b1220", color: "#d0d5dd", borderRadius: 8, padding: 12, fontSize: 12, overflow: "auto", fontFamily: "JetBrains Mono, monospace" }}
                  >
                    {JSON.stringify(q.data.dsl_recipe, null, 2)}
                  </pre>
                </details>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

const toolbar: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "12px 18px",
  borderBottom: "1px solid #eaecf0",
  background: "#fff",
  flexShrink: 0,
};
const toolbarTitle: CSSProperties = {
  flex: 1,
  minWidth: 0,
  fontSize: 13,
  fontWeight: 700,
  color: "#344054",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};
const kindPill: CSSProperties = {
  flexShrink: 0,
  fontSize: 11,
  fontWeight: 800,
  letterSpacing: "0.06em",
  color: "#4a6785",
  background: "#eef1f5",
  borderRadius: 999,
  padding: "4px 9px",
};
const closeBtn: CSSProperties = {
  flexShrink: 0,
  border: "1px solid #d0d5dd",
  background: "#fff",
  borderRadius: 8,
  padding: "6px 12px",
  fontSize: 12,
  fontWeight: 700,
  color: "#344054",
  cursor: "pointer",
};
const page: CSSProperties = {
  padding: "20px 24px 28px",
};
const entryUrl: CSSProperties = {
  fontSize: 12,
  color: "#667085",
  wordBreak: "break-all",
  marginBottom: 14,
};
const btnPrimary: CSSProperties = { border: "none", borderRadius: 999, padding: "8px 16px", background: "#175cd3", color: "#fff", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const btnDanger: CSSProperties = { border: "1px solid #fecdca", borderRadius: 999, padding: "8px 16px", background: "#fff", color: "#b42318", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const infoBox: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 16, color: "#667085", fontSize: 13 };
const sourceCode: CSSProperties = { margin: 0, maxHeight: 520, overflow: "auto", whiteSpace: "pre", background: "#0b1220", color: "#d0d5dd", borderRadius: 8, padding: 12, fontSize: 12, lineHeight: 1.55, fontFamily: "JetBrains Mono, ui-monospace, monospace" };
