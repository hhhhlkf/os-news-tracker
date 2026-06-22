import type { AgentRunRecord, AgentRunStage, AgentSource } from "../types";
import { isAgentSourceRunning } from "../pages/homeData";

interface AgentSourceRunCardProps {
  source: AgentSource;
  latestRun?: AgentRunRecord | null;
  isTriggering?: boolean;
  errorMessage?: string | null;
  onTrigger: (sourceId: number) => Promise<void> | void;
}

const stageLabels: Record<AgentRunStage, string> = {
  planning: "规划 URL",
  crawling: "并行抓取",
  quality: "质量筛选",
  summarizing: "生成摘要",
  completed: "已完成",
  failed: "失败",
};

export function formatAgentStageLabel(stage?: AgentRunStage | null): string {
  if (!stage) return "尚未运行";
  return stageLabels[stage] ?? stage;
}

export function formatAgentRunStats(run?: AgentRunRecord | null): string {
  if (!run) return "暂无运行记录";
  if (run.current_stage === "planning") return `已规划 ${run.plan_urls_count} 个 URL`;
  if (run.current_stage === "crawling") return `已抓取 ${run.fetched_count} / ${run.plan_urls_count}`;
  if (run.current_stage === "quality") return `质量通过 ${run.quality_passed} / ${run.fetched_count}`;
  if (run.current_stage === "summarizing") return `已生成 ${run.items_created} / ${run.quality_passed}`;
  return `规划 ${run.plan_urls_count} · 抓取 ${run.fetched_count} · 通过 ${run.quality_passed} · 候选 ${run.items_created}`;
}

function formatList(values: string[] | undefined): string {
  if (!values || values.length === 0) return "—";
  return values.join(" / ");
}

export function AgentSourceRunCard(props: AgentSourceRunCardProps) {
  const { source, latestRun, isTriggering = false, errorMessage, onTrigger } = props;
  const running = isAgentSourceRunning(latestRun);
  const stageLabel = formatAgentStageLabel(latestRun?.current_stage);
  const buttonDisabled = isTriggering || running;

  return (
    <article
      style={{
        border: "1px solid #d0d5dd",
        borderRadius: 10,
        background: "#fcfcfd",
        padding: 16,
        display: "grid",
        gap: 12,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div style={{ display: "grid", gap: 6 }}>
          <div style={{ fontSize: 16, fontWeight: 700, color: "#101828" }}>{source.name}</div>
          <div style={{ fontSize: 12, color: "#667085", wordBreak: "break-all" }}>{source.url}</div>
          <div style={{ fontSize: 13, color: "#344054" }}>
            聚焦：{formatList(source.config?.focus_areas)} · 主题：{formatList(source.config?.topic_groups)}
          </div>
        </div>
        <button
          type="button"
          disabled={buttonDisabled}
          onClick={() => void onTrigger(source.id)}
          style={{
            border: "none",
            borderRadius: 999,
            padding: "10px 14px",
            minWidth: 104,
            background: buttonDisabled ? "#98a2b3" : "#175cd3",
            color: "#ffffff",
            fontSize: 13,
            fontWeight: 700,
            cursor: buttonDisabled ? "not-allowed" : "pointer",
          }}
        >
          {running ? "运行中" : isTriggering ? "提交中" : "立即抓取"}
        </button>
      </div>

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))",
          gap: 10,
        }}
      >
        <Metric label="当前阶段" value={stageLabel} />
        <Metric label="规划 URL" value={String(latestRun?.plan_urls_count ?? 0)} />
        <Metric label="已抓取" value={String(latestRun?.fetched_count ?? 0)} />
        <Metric label="质量通过" value={String(latestRun?.quality_passed ?? 0)} />
        <Metric label="候选条数" value={String(latestRun?.items_created ?? 0)} />
      </div>

      <div style={{ fontSize: 13, color: "#475467" }}>
        {latestRun?.stage_message ?? formatAgentRunStats(latestRun)}
      </div>

      {(latestRun?.error_message || errorMessage) && (
        <div
          style={{
            borderRadius: 8,
            background: "#fef3f2",
            color: "#b42318",
            padding: "10px 12px",
            fontSize: 13,
          }}
        >
          {errorMessage ?? latestRun?.error_message}
        </div>
      )}
    </article>
  );
}

function Metric(props: { label: string; value: string }) {
  return (
    <div style={{ border: "1px solid #eaecf0", borderRadius: 8, background: "#ffffff", padding: 10 }}>
      <div style={{ fontSize: 12, color: "#667085", marginBottom: 4 }}>{props.label}</div>
      <div style={{ fontSize: 14, fontWeight: 700, color: "#101828" }}>{props.value}</div>
    </div>
  );
}
