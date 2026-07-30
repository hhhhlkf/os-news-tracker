import { useState, type CSSProperties } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchTrendEmbeddingStatus, prepareTrendEmbeddingModel } from "./api";
import type { TrendEmbeddingStatus, TrendEmbeddingStatusValue } from "./types";

const embeddingStatusQueryKey = ["trends", "embedding-status"] as const;
const activeStatuses: TrendEmbeddingStatusValue[] = ["downloading", "loading", "processing"];
const collapseStorageKey = "os-news-tracker:trends:embedding-panel-collapsed";

const statusLabels: Record<TrendEmbeddingStatusValue, string> = {
  not_installed: "未安装",
  downloading: "下载中",
  ready: "就绪",
  loading: "加载中",
  processing: "生成中",
  failed: "失败",
};

const statusTones: Record<TrendEmbeddingStatusValue, CSSProperties> = {
  not_installed: { background: "#f2f4f7", color: "#475467" },
  downloading: { background: "#eff8ff", color: "#175cd3" },
  ready: { background: "#ecfdf3", color: "#067647" },
  loading: { background: "#eff8ff", color: "#175cd3" },
  processing: { background: "#eff8ff", color: "#175cd3" },
  failed: { background: "#fef3f2", color: "#b42318" },
};

function workerLabel(status: TrendEmbeddingStatus): string {
  if (!status.worker_reachable) return "Worker 无法连接";
  return status.worker_active ? "Worker 运行中" : "Worker 空闲";
}

function readCollapsedState(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(collapseStorageKey) === "true";
}

export function TrendEmbeddingPanel() {
  const [isCollapsed, setIsCollapsed] = useState(readCollapsedState);
  const toggleCollapsed = () => {
    setIsCollapsed((collapsed) => {
      const next = !collapsed;
      window.localStorage.setItem(collapseStorageKey, String(next));
      return next;
    });
  };
  const queryClient = useQueryClient();
  const statusQuery = useQuery({
    queryKey: embeddingStatusQueryKey,
    queryFn: fetchTrendEmbeddingStatus,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && activeStatuses.includes(status) ? 2000 : false;
    },
  });
  const prepareMutation = useMutation({
    mutationFn: prepareTrendEmbeddingModel,
    onSuccess: (status) => queryClient.setQueryData(embeddingStatusQueryKey, status),
  });

  const status = statusQuery.data;
  const busy = status ? activeStatuses.includes(status.status) : false;
  const error = (statusQuery.error ?? prepareMutation.error) as Error | null;

  return (
    <section style={panel}>
      <div style={header}>
        <div>
          <div style={sectionTitle}>Embedding 模型</div>
          <div style={sectionCopy}>新闻卡片向量由独立的单实例 CPU Worker 生成，模型权重存放在持久化缓存卷中。</div>
        </div>
        <div style={headerActions}>
          {!isCollapsed && (
            <button
              type="button"
              style={busy || prepareMutation.isPending || status?.worker_reachable === false ? disabledButton : primaryButton}
              onClick={() => prepareMutation.mutate()}
              disabled={busy || prepareMutation.isPending || statusQuery.isLoading || status?.worker_reachable === false}
            >
              {busy ? "Worker 运行中…" : status?.cache_installed ? "重新准备模型" : "准备模型"}
            </button>
          )}
          <button
            type="button"
            style={collapseButton}
            onClick={toggleCollapsed}
            aria-expanded={!isCollapsed}
            aria-label={isCollapsed ? "展开 Embedding 模型" : "收起 Embedding 模型"}
          >
            {isCollapsed ? "展开" : "收起"}
          </button>
        </div>
      </div>

      {!isCollapsed && <>
        {statusQuery.isLoading && <div style={empty}>正在读取 Embedding 状态…</div>}
        {error && <div style={errorBox}>{error.message}</div>}

        {status && (
          <>
          <div style={statusRow}>
            <span style={{ ...statusBadge, ...statusTones[status.status] }}>{statusLabels[status.status]}</span>
            <span style={statusMeta}>
              {workerLabel(status)} · {status.dimension} 维 {status.normalization.toUpperCase()} 归一化 · 更新于{" "}
              {formatTimestamp(status.status_updated_at)}
            </span>
          </div>

          {status.worker_error && (
            <div style={warningBox}>
              <div style={warningTitle}>Embedding Worker 不可用</div>
              <div style={warningDetail}>{status.worker_error}</div>
            </div>
          )}

          {status.status === "not_installed" && status.worker_reachable && (
            <div style={emptyState}>
              <div style={emptyTitle}>本机尚未安装 {status.model_id}</div>
              <div>
                点击“准备模型”后，Worker 会把官方模型（约 1.2 GB）下载到缓存卷 {status.cache_dir}；下载完成前不会生成任何向量。
              </div>
            </div>
          )}

          {status.status === "failed" && (
            <div style={failureBox}>
              <div style={failureTitle}>{status.error_message ?? "Embedding 任务失败。"}</div>
              {status.remedy && <div style={failureRemedy}>处理建议：{status.remedy}</div>}
            </div>
          )}

          <dl style={grid}>
            <Field label="模型" value={status.model_id} />
            <Field
              label="模型版本"
              value={status.model_version ? status.model_version.slice(0, 12) : `${status.model_revision}（未下载）`}
              title={status.model_version ?? undefined}
            />
            <Field label="向量版本" value={status.embedding_version} />
            <Field label="Worker 地址" value={status.worker_base_url} />
          </dl>
          </>
        )}
      </>}
    </section>
  );
}

function Field({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div style={fieldCell}>
      <dt style={fieldLabel}>{label}</dt>
      <dd style={monoValue} title={title ?? value}>
        {value}
      </dd>
    </div>
  );
}

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

const panel: CSSProperties = {
  marginBottom: 14,
  border: "1px solid #eaecf0",
  borderRadius: 12,
  padding: 20,
  background: "#fff",
  boxShadow: "0 1px 2px rgba(16,24,40,.04)",
};
const header: CSSProperties = { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 12 };
const headerActions: CSSProperties = { display: "flex", alignItems: "center", gap: 8, flex: "0 0 auto" };
const sectionTitle: CSSProperties = { color: "#101828", fontSize: 16, fontWeight: 800 };
const sectionCopy: CSSProperties = { color: "#667085", fontSize: 12, marginTop: 5, lineHeight: 1.6, maxWidth: 720 };
const primaryButton: CSSProperties = { border: "1px solid #175cd3", borderRadius: 8, background: "#175cd3", color: "#fff", padding: "8px 12px", fontSize: 12, fontWeight: 800, cursor: "pointer", whiteSpace: "nowrap" };
const disabledButton: CSSProperties = { ...primaryButton, border: "1px solid #a4bcfd", background: "#eff4ff", color: "#84adff", cursor: "not-allowed" };
const collapseButton: CSSProperties = { border: "1px solid #d0d5dd", borderRadius: 8, background: "#fff", color: "#344054", padding: "8px 10px", fontSize: 12, fontWeight: 800, cursor: "pointer", whiteSpace: "nowrap" };
const statusRow: CSSProperties = { display: "flex", alignItems: "center", gap: 10, marginTop: 16, flexWrap: "wrap" };
const statusBadge: CSSProperties = { borderRadius: 999, padding: "4px 10px", fontSize: 12, fontWeight: 800 };
const statusMeta: CSSProperties = { color: "#667085", fontSize: 12 };
const grid: CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 12, margin: "16px 0 0" };
const fieldCell: CSSProperties = { border: "1px solid #f2f4f7", borderRadius: 10, padding: "10px 12px", background: "#fcfcfd", minWidth: 0 };
const fieldLabel: CSSProperties = { color: "#98a2b3", fontSize: 11, fontWeight: 700 };
const fieldValue: CSSProperties = { color: "#344054", fontSize: 13, fontWeight: 600, margin: "5px 0 0", overflowWrap: "anywhere" };
const monoValue: CSSProperties = { ...fieldValue, fontFamily: "var(--font-mono)", fontSize: 12, fontWeight: 500 };
const empty: CSSProperties = { color: "#98a2b3", textAlign: "center", padding: 24, fontSize: 13 };
const emptyState: CSSProperties = { marginTop: 14, border: "1px dashed #d0d5dd", borderRadius: 10, color: "#667085", textAlign: "center", padding: "24px 20px", fontSize: 13, lineHeight: 1.65 };
const emptyTitle: CSSProperties = { color: "#344054", fontWeight: 800, marginBottom: 5 };
const errorBox: CSSProperties = { marginTop: 14, border: "1px solid #fecdca", borderRadius: 8, background: "#fef3f2", color: "#b42318", padding: "8px 10px", fontSize: 12 };
const warningBox: CSSProperties = { marginTop: 14, border: "1px solid #fec84b", borderRadius: 10, background: "#fffaeb", padding: "12px 14px" };
const warningTitle: CSSProperties = { color: "#b54708", fontSize: 13, fontWeight: 800 };
const warningDetail: CSSProperties = { color: "#93370d", fontSize: 12, marginTop: 6, lineHeight: 1.6 };
const failureBox: CSSProperties = { marginTop: 14, border: "1px solid #fecdca", borderRadius: 10, background: "#fef3f2", padding: "12px 14px" };
const failureTitle: CSSProperties = { color: "#b42318", fontSize: 13, fontWeight: 700, lineHeight: 1.6 };
const failureRemedy: CSSProperties = { color: "#912018", fontSize: 12, marginTop: 6, lineHeight: 1.6 };
