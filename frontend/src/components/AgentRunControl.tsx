import { AgentSourceRunCard } from "./AgentSourceRunCard";
import type { AgentRunRecord, AgentSource } from "../types";

interface AgentRunControlProps {
  sources: AgentSource[];
  runsBySourceId: Record<number, AgentRunRecord[] | undefined>;
  isLoading?: boolean;
  errorMessage?: string | null;
  triggerPendingSourceId?: number | null;
  triggerErrors?: Record<number, string | null | undefined>;
  onTrigger: (sourceId: number) => Promise<void> | void;
}

export function AgentRunControl(props: AgentRunControlProps) {
  const {
    sources,
    runsBySourceId,
    isLoading = false,
    errorMessage,
    triggerPendingSourceId = null,
    triggerErrors = {},
    onTrigger,
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

  if (sources.length === 0) {
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
