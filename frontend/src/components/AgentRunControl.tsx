import type { CSSProperties } from "react";
import { AgentSourceRunCard } from "./AgentSourceRunCard";
import type { AgentRunRecord, AgentSource, AgentSourceCandidatesResponse } from "../types";

const paginationButtonStyle: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 999,
  padding: "8px 12px",
  minWidth: 88,
  background: "#ffffff",
  color: "#344054",
  fontSize: 12,
  fontWeight: 600,
  cursor: "pointer",
};

interface AgentRunControlProps {
  sources: AgentSource[];
  candidatePage?: AgentSourceCandidatesResponse | null;
  runsBySourceId: Record<number, AgentRunRecord[] | undefined>;
  isLoading?: boolean;
  errorMessage?: string | null;
  triggerPendingSourceId?: number | null;
  triggerErrors?: Record<number, string | null | undefined>;
  cancelPendingSourceId?: number | null;
  deletePendingSourceId?: number | null;
  candidateTriggerPendingSourceId?: number | null;
  candidateTriggerErrors?: Record<number, string | null | undefined>;
  onTrigger: (sourceId: number) => Promise<void> | void;
  onCancel?: (sourceId: number, runId: number) => Promise<void> | void;
  onDelete?: (sourceId: number) => Promise<void> | void;
  onTriggerCandidate?: (sourceId: number) => Promise<void> | void;
  onCandidatePageChange?: (page: number) => void;
}

export function AgentRunControl(props: AgentRunControlProps) {
  const {
    sources,
    candidatePage = null,
    runsBySourceId,
    isLoading = false,
    errorMessage,
    triggerPendingSourceId = null,
    triggerErrors = {},
    cancelPendingSourceId = null,
    deletePendingSourceId = null,
    candidateTriggerPendingSourceId = null,
    candidateTriggerErrors = {},
    onTrigger,
    onCancel,
    onDelete,
    onTriggerCandidate,
    onCandidatePageChange,
  } = props;
  const candidateSources = candidatePage?.items ?? [];

  if (isLoading) {
    return <div style={{ fontSize: 13, color: "#667085" }}>正在加载 Agent 源…</div>;
  }

  if (errorMessage) {
    return (
      <div style={{ fontSize: 13, color: "#b42318" }}>
        {errorMessage}
      </div>
    );
  }

  if (sources.length === 0 && candidateSources.length === 0) {
    return (
      <div
        style={{
          border: "1px dashed #d0d5dd",
          borderRadius: 8,
          padding: 16,
          color: "#667085",
          background: "#fcfcfd",
          fontSize: 13,
        }}
      >
        当前还没有可用的 Agent Crawl source。
      </div>
    );
  }

  return (
    <div style={{ display: "grid", gap: 12 }}>
      {candidateSources.length > 0 && (
        <section
          style={{
            border: "1px solid #d0d5dd",
            borderRadius: 10,
            background: "#f8fafc",
            padding: 16,
            display: "grid",
            gap: 10,
          }}
        >
          <div style={{ display: "grid", gap: 4 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>标准抓取来源</div>
            <div style={{ fontSize: 13, color: "#667085" }}>
              直接复用标准抓取链接，一键创建或复用对应 Agent source 并立即运行。
            </div>
            {candidatePage && (
              <div style={{ fontSize: 12, color: "#475467" }}>
                共 {candidatePage.total} 条 · 第 {candidatePage.page} / {candidatePage.total_pages} 页
              </div>
            )}
          </div>
          <div style={{ display: "grid", gap: 10 }}>
            {candidateSources.map((source) => (
              <div
                key={source.id}
                style={{
                  border: "1px solid #eaecf0",
                  borderRadius: 8,
                  background: "#ffffff",
                  padding: 12,
                  display: "flex",
                  gap: 12,
                  justifyContent: "space-between",
                  alignItems: "center",
                  flexWrap: "wrap",
                }}
              >
                <div style={{ display: "grid", gap: 4 }}>
                  <div style={{ fontSize: 14, fontWeight: 700, color: "#101828" }}>{source.name}</div>
                  <div style={{ fontSize: 12, color: "#667085", wordBreak: "break-all" }}>{source.url}</div>
                  <div style={{ fontSize: 12, color: "#475467" }}>
                    {source.source_type} · {source.main_category ?? "未分类"}
                  </div>
                  {candidateTriggerErrors[source.id] && (
                    <div style={{ fontSize: 12, color: "#b42318" }}>{candidateTriggerErrors[source.id]}</div>
                  )}
                </div>
                <button
                  type="button"
                  disabled={candidateTriggerPendingSourceId === source.id}
                  onClick={() => void onTriggerCandidate?.(source.id)}
                  style={{
                    border: "none",
                    borderRadius: 999,
                    padding: "10px 14px",
                    minWidth: 132,
                    background: candidateTriggerPendingSourceId === source.id ? "#98a2b3" : "#175cd3",
                    color: "#ffffff",
                    fontSize: 13,
                    fontWeight: 700,
                    cursor: candidateTriggerPendingSourceId === source.id ? "not-allowed" : "pointer",
                  }}
                >
                  {candidateTriggerPendingSourceId === source.id ? "提交中" : "一键 Agent 运行"}
                </button>
              </div>
            ))}
          </div>
          {candidatePage && candidatePage.total_pages > 1 && (
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
              <button
                type="button"
                disabled={candidatePage.page <= 1}
                onClick={() => onCandidatePageChange?.(candidatePage.page - 1)}
                style={{
                  ...paginationButtonStyle,
                  cursor: candidatePage.page <= 1 ? "not-allowed" : "pointer",
                  color: candidatePage.page <= 1 ? "#98a2b3" : "#344054",
                }}
              >
                上一页
              </button>
              <button
                type="button"
                disabled={candidatePage.page >= candidatePage.total_pages}
                onClick={() => onCandidatePageChange?.(candidatePage.page + 1)}
                style={{
                  ...paginationButtonStyle,
                  cursor: candidatePage.page >= candidatePage.total_pages ? "not-allowed" : "pointer",
                  color: candidatePage.page >= candidatePage.total_pages ? "#98a2b3" : "#344054",
                }}
              >
                下一页
              </button>
            </div>
          )}
        </section>
      )}

      {sources.map((source) => (
        <AgentSourceRunCard
          key={source.id}
          source={source}
          latestRun={runsBySourceId[source.id]?.[0] ?? null}
          isTriggering={triggerPendingSourceId === source.id}
          isCancelling={cancelPendingSourceId === source.id}
          isDeleting={deletePendingSourceId === source.id}
          errorMessage={triggerErrors[source.id] ?? null}
          onTrigger={onTrigger}
          onCancel={onCancel}
          onDelete={onDelete}
        />
      ))}
    </div>
  );
}
