import type { CSSProperties } from "react";
import type { ReactNode } from "react";
import { useState } from "react";
import { AgentSourceRunCard } from "./AgentSourceRunCard";
import { TimeRangePicker } from "./TimeRangePicker";
import type {
  AgentRunRecord,
  AgentSource,
  AgentSourceCandidatesResponse,
  ManualNewsRelativeRange,
  ManualNewsTimeMode,
} from "../types";

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
  agentTimeMode?: ManualNewsTimeMode;
  agentRelativeRange?: ManualNewsRelativeRange;
  agentStartDate?: string;
  agentEndDate?: string;
  sourceManagerContent?: ReactNode;
  onTrigger: (sourceId: number) => Promise<void> | void;
  onCancel?: (sourceId: number, runId: number) => Promise<void> | void;
  onDelete?: (sourceId: number) => Promise<void> | void;
  onTriggerCandidate?: (sourceId: number) => Promise<void> | void;
  onCandidatePageChange?: (page: number) => void;
  onAgentTimeModeChange?: (value: ManualNewsTimeMode) => void;
  onAgentRelativeRangeChange?: (value: ManualNewsRelativeRange) => void;
  onAgentStartDateChange?: (value: string) => void;
  onAgentEndDateChange?: (value: string) => void;
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
    agentTimeMode = "relative",
    agentRelativeRange = "7d",
    agentStartDate = "",
    agentEndDate = "",
    sourceManagerContent,
    onTrigger,
    onCancel,
    onDelete,
    onTriggerCandidate,
    onCandidatePageChange,
    onAgentTimeModeChange = () => {},
    onAgentRelativeRangeChange = () => {},
    onAgentStartDateChange = () => {},
    onAgentEndDateChange = () => {},
  } = props;
  const candidateSources = candidatePage?.items ?? [];
  const [timeExpanded, setTimeExpanded] = useState(false);
  const [candidatesExpanded, setCandidatesExpanded] = useState(false);

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
      <section
        style={{
          border: "1px solid #d0d5dd",
          borderRadius: 8,
          background: "linear-gradient(135deg, #ffffff 0%, #f8fafc 100%)",
          padding: 14,
          display: "grid",
          gap: 10,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "flex-start", flexWrap: "wrap" }}>
          <div style={{ display: "grid", gap: 4 }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>Agent 时间范围</div>
            <div style={{ fontSize: 13, color: "#667085" }}>
              RSS Agent 只会深抓这个时间范围内的 feed 条目。
            </div>
          </div>
          <button type="button" onClick={() => setTimeExpanded((value) => !value)} style={sectionToggleStyle}>
            {timeExpanded ? "收起" : "展开"}
          </button>
        </div>
        {timeExpanded && (
          <TimeRangePicker
            timeMode={agentTimeMode}
            relativeRange={agentRelativeRange}
            startDate={agentStartDate}
            endDate={agentEndDate}
            disabled={candidateTriggerPendingSourceId !== null || triggerPendingSourceId !== null}
            onTimeModeChange={onAgentTimeModeChange}
            onRelativeRangeChange={onAgentRelativeRangeChange}
            onStartDateChange={onAgentStartDateChange}
            onEndDateChange={onAgentEndDateChange}
          />
        )}
      </section>

      {sourceManagerContent}

      {candidateSources.length > 0 && (
        <section
          style={{
            border: "1px solid #d0d5dd",
            borderRadius: 10,
            background: "linear-gradient(135deg, #eef2f6 0%, #f8fafc 100%)",
            padding: 16,
            display: "grid",
            gap: 10,
          }}
        >
          <div style={{ display: "flex", justifyContent: "space-between", gap: 10, alignItems: "flex-start", flexWrap: "wrap" }}>
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
            <button type="button" onClick={() => setCandidatesExpanded((value) => !value)} style={sectionToggleStyle}>
              {candidatesExpanded ? "收起" : "展开"}
            </button>
          </div>
          {candidatesExpanded && (
            <>
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
            </>
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

const sectionToggleStyle: CSSProperties = {
  border: "1px solid #d0d5dd",
  borderRadius: 999,
  padding: "8px 12px",
  minWidth: 72,
  background: "linear-gradient(135deg, #ffffff 0%, #f2f4f7 100%)",
  color: "#344054",
  fontSize: 12,
  fontWeight: 700,
  cursor: "pointer",
};
