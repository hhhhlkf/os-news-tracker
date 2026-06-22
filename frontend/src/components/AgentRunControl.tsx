import { AgentSourceRunCard } from "./AgentSourceRunCard";
import type { AgentRunRecord, AgentSource, AgentSourceCandidate } from "../types";

interface AgentRunControlProps {
  sources: AgentSource[];
  candidateSources?: AgentSourceCandidate[];
  runsBySourceId: Record<number, AgentRunRecord[] | undefined>;
  isLoading?: boolean;
  errorMessage?: string | null;
  triggerPendingSourceId?: number | null;
  triggerErrors?: Record<number, string | null | undefined>;
  candidateTriggerPendingSourceId?: number | null;
  candidateTriggerErrors?: Record<number, string | null | undefined>;
  onTrigger: (sourceId: number) => Promise<void> | void;
  onTriggerCandidate?: (sourceId: number) => Promise<void> | void;
}

export function AgentRunControl(props: AgentRunControlProps) {
  const {
    sources,
    candidateSources = [],
    runsBySourceId,
    isLoading = false,
    errorMessage,
    triggerPendingSourceId = null,
    triggerErrors = {},
    candidateTriggerPendingSourceId = null,
    candidateTriggerErrors = {},
    onTrigger,
    onTriggerCandidate,
  } = props;

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
        </section>
      )}

      {sources.map((source) => (
        <AgentSourceRunCard
          key={source.id}
          source={source}
          latestRun={runsBySourceId[source.id]?.[0] ?? null}
          isTriggering={triggerPendingSourceId === source.id}
          errorMessage={triggerErrors[source.id] ?? null}
          onTrigger={onTrigger}
        />
      ))}
    </div>
  );
}
