import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { TimeRangePicker } from "./TimeRangePicker";
import type {
  ManualNewsRelativeRange,
  ManualNewsRunRequest,
  ManualNewsRunState,
  ManualNewsRunStatus,
  ManualNewsTimeMode,
} from "../types";

export interface NewsRunFormState {
  timeMode: ManualNewsTimeMode;
  relativeRange: ManualNewsRelativeRange;
  startDate: string;
  endDate: string;
  targetCount: string;
}

interface NewsRunControlProps {
  mode?: "standard" | "agent";
  onModeChange?: (mode: "standard" | "agent") => void;
  agentContent?: ReactNode;
  status?: ManualNewsRunStatus;
  isLoading: boolean;
  errorMessage?: string | null;
  isSubmitting?: boolean;
  onStart: (request: ManualNewsRunRequest) => Promise<void> | void;
  onStop: () => Promise<void> | void;
}

const stateLabels: Record<ManualNewsRunState, string> = {
  idle: "空闲",
  collecting: "收集中",
  processing: "处理中",
  stopping: "停止中",
  completed: "已完成",
  failed: "失败",
  stopped: "已停止",
};

export function isNewsRunBusy(state: ManualNewsRunState) {
  return state === "collecting" || state === "processing" || state === "stopping";
}

export function shouldAutoExpandNewsRunControl(state?: ManualNewsRunState) {
  return state === "collecting" || state === "processing" || state === "stopping" || state === "failed";
}

export function buildNewsRunFormState(status?: ManualNewsRunStatus): NewsRunFormState {
  return {
    timeMode: status?.time_mode ?? "relative",
    relativeRange: status?.relative_range ?? "7d",
    startDate: toDateInputValue(status?.start_at),
    endDate: toDateInputValue(status?.end_at),
    targetCount: String(status?.target_count ?? 50),
  };
}

export function formatNewsRunWindowLabel(status?: ManualNewsRunStatus) {
  if (!status?.time_mode) {
    return "尚未设置";
  }
  if (status.time_mode === "relative") {
    return `最近 ${status.relative_range ?? "7d"}`;
  }
  const start = toDateInputValue(status.start_at) || "--";
  const end = toDateInputValue(status.end_at) || "--";
  return `${start} 至 ${end}`;
}

function toDateInputValue(value: string | null | undefined) {
  if (!value) {
    return "";
  }
  return value.slice(0, 10);
}

export function toAbsoluteDateTime(value: string, endOfDay: boolean): string | null {
  if (!value) {
    return null;
  }
  return `${value}T${endOfDay ? "23:59:59.999" : "00:00:00.000"}Z`;
}

export function NewsRunControl(props: NewsRunControlProps) {
  const {
    mode = "standard",
    onModeChange,
    agentContent,
    status,
    isLoading,
    errorMessage,
    isSubmitting = false,
    onStart,
    onStop,
  } = props;
  const [formState, setFormState] = useState<NewsRunFormState>(() => buildNewsRunFormState(status));
  const [localError, setLocalError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(() => shouldAutoExpandNewsRunControl(status?.state));

  useEffect(() => {
    if (!status || isNewsRunBusy(status.state)) return;
    if (status.state === "idle") {
      setFormState(buildNewsRunFormState(status));
    } else {
      setFormState(buildNewsRunFormState(undefined));
    }
  }, [
    status?.state,
    status?.time_mode,
    status?.relative_range,
    status?.start_at,
    status?.end_at,
    status?.target_count,
  ]);

  useEffect(() => {
    if (shouldAutoExpandNewsRunControl(status?.state)) {
      setExpanded(true);
    }
  }, [status?.state]);

  const busy = status ? isNewsRunBusy(status.state) : false;
  const disabled = busy || isSubmitting;
  const isAgentMode = mode === "agent";

  const progressLabel = useMemo(() => {
    if (!status) return "等待状态";
    const saved = status.saved_count;
    const target = status.target_count ?? "?";
    if (status.state === "collecting") {
      return `入库 ${saved} / 目标 ${target}  ·  收集候选 ${status.queued_count} 条`;
    }
    if (status.state === "processing") {
      return `入库 ${saved} / 目标 ${target}  ·  已处理 ${status.processed_count} 条`;
    }
    return `入库 ${saved} / 目标 ${target}`;
  }, [status]);

  const fulfilledLabel = useMemo(() => {
    if (!status || status.state === "idle") return "—";
    if (status.state === "completed" || status.state === "failed" || status.state === "stopped") {
      return status.fulfilled ? "✓ 已达标" : "✗ 未达标";
    }
    if (status.saved_count >= (status.target_count ?? Infinity)) return "✓ 已达标";
    const saved = status.saved_count;
    const target = status.target_count ?? "?";
    return `进行中 (${saved}/${target})`;
  }, [status]);

  const collectingHint = useMemo(() => {
    if (!status || status.state !== "collecting") return null;
    const remaining = (status.target_count ?? 0) - status.saved_count;
    if (remaining > 0) {
      return `正在继续抓取/补充候选… 尚需入库 ${remaining} 条`;
    }
    return null;
  }, [status]);

  const runButtonLabel =
    !status ||
    status.state === "idle" ||
    status.state === "completed" ||
    status.state === "stopped" ||
    status.state === "failed"
      ? "开始处理"
      : status.state === "stopping"
        ? "停止中"
        : "结束处理";

  async function handlePrimaryAction() {
    setLocalError(null);
    if (status && isNewsRunBusy(status.state)) {
      await onStop();
      return;
    }

    const targetCount = Number(formState.targetCount);
    if (!Number.isFinite(targetCount) || targetCount <= 0) {
      setLocalError("目标条目数需要大于 0");
      return;
    }

    if (formState.timeMode === "absolute" && (!formState.startDate || !formState.endDate)) {
      setLocalError("绝对范围需要同时选择开始和结束日期");
      return;
    }

    await onStart({
      time_mode: formState.timeMode,
      relative_range: formState.timeMode === "relative" ? formState.relativeRange : null,
      start_at: formState.timeMode === "absolute" ? toAbsoluteDateTime(formState.startDate, false) : null,
      end_at: formState.timeMode === "absolute" ? toAbsoluteDateTime(formState.endDate, true) : null,
      target_count: targetCount,
    });
  }

  return (
    <section
      style={{
        border: "1px solid #d0d5dd",
        borderRadius: 8,
        background: "#ffffff",
        padding: 18,
        marginBottom: 16,
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: 16,
          flexWrap: "wrap",
          marginBottom: 14,
        }}
      >
        <div style={{ display: "grid", gap: 6 }}>
          <div style={{ fontSize: 18, fontWeight: 700, color: "#101828" }}>新闻处理控制</div>
          <div style={{ fontSize: 13, color: "#667085" }}>
            {isAgentMode
              ? "Agent Crawl 单源触发：在首页按阶段观察规划、抓取、质量筛选和摘要生成。"
              : "目标驱动新闻采集：尽量抓满目标条目数，再统一入库。"}
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", justifyContent: "flex-end" }}>
          <div
            style={{
              display: "inline-flex",
              padding: 4,
              border: "1px solid #d0d5dd",
              borderRadius: 999,
              background: "#f8fafc",
            }}
          >
            {(["standard", "agent"] as const).map((option) => {
              const selected = option === mode;
              return (
                <button
                  key={option}
                  type="button"
                  onClick={() => onModeChange?.(option)}
                  style={{
                    border: "none",
                    borderRadius: 999,
                    padding: "8px 12px",
                    minWidth: 92,
                    background: selected ? "#ffffff" : "transparent",
                    color: selected ? "#101828" : "#667085",
                    boxShadow: selected ? "0 1px 2px rgba(16, 24, 40, 0.08)" : "none",
                    fontSize: 13,
                    fontWeight: 700,
                    cursor: "pointer",
                  }}
                >
                  {option === "standard" ? "标准抓取" : "Agent Crawl"}
                </button>
              );
            })}
          </div>
          {!isAgentMode && (
            <>
              <button
                type="button"
                onClick={() => setExpanded((value) => !value)}
                style={{
                  border: "1px solid #d0d5dd",
                  borderRadius: 999,
                  padding: "10px 14px",
                  minWidth: 92,
                  background: "#ffffff",
                  color: "#344054",
                  fontSize: 13,
                  fontWeight: 700,
                  cursor: "pointer",
                }}
              >
                {expanded ? "收起设置" : "展开设置"}
              </button>
              <button
                type="button"
                onClick={() => void handlePrimaryAction()}
                disabled={isSubmitting || status?.state === "stopping"}
                style={{
                  border: "none",
                  borderRadius: 999,
                  padding: "12px 18px",
                  minWidth: 116,
                  background: status && isNewsRunBusy(status.state) ? "#f04438" : "#175cd3",
                  color: "#ffffff",
                  fontSize: 14,
                  fontWeight: 700,
                  cursor: isSubmitting || status?.state === "stopping" ? "not-allowed" : "pointer",
                  opacity: isSubmitting || status?.state === "stopping" ? 0.7 : 1,
                }}
              >
                {runButtonLabel}
              </button>
            </>
          )}
        </div>
      </div>

      {isAgentMode ? (
        <div style={{ display: "grid", gap: 12 }}>
          {(errorMessage || localError) && (
            <div
              style={{
                border: "1px solid #fecdca",
                background: "#fef3f2",
                color: "#b42318",
                borderRadius: 8,
                padding: "10px 12px",
                fontSize: 13,
              }}
            >
              {localError ?? errorMessage}
            </div>
          )}
          {agentContent}
        </div>
      ) : (
        <div style={{ display: "grid", gap: 16 }}>
          {!expanded && (
            <div style={{ fontSize: 13, color: "#667085" }}>
              {status ? stateLabels[status.state] : isLoading ? "加载中" : "空闲"}
              {" · "}
              {formatNewsRunWindowLabel(status)}
              {" · "}
              {progressLabel}
              {status?.state === "completed" || status?.state === "failed" || status?.state === "stopped"
                ? ` · ${status.fulfilled ? "已达标" : "未达标"}`
                : ""}
            </div>
          )}

          <div
            style={{
              maxHeight: expanded ? "600px" : "0",
              opacity: expanded ? 1 : 0,
              overflow: "hidden",
              transition: "max-height 0.35s ease, opacity 0.3s ease",
            }}
          >
            <div style={{ display: "grid", gap: 16 }}>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))",
                  gap: 12,
                }}
              >
                <StatusBlock label="当前状态" value={status ? stateLabels[status.state] : isLoading ? "加载中" : "空闲"} />
                <StatusBlock label="时间范围" value={formatNewsRunWindowLabel(status)} />
                <StatusBlock label="目标条目数" value={status?.target_count ? String(status.target_count) : formState.targetCount} />
                <StatusBlock label="原始发现" value={status ? String(status.discovered_count) : "0"} />
                <StatusBlock label="入队候选" value={status ? String(status.queued_count) : "0"} />
                <StatusBlock label="已处理" value={status ? String(status.processed_count) : "0"} />
                <StatusBlock label="新增入库" value={status ? String(status.saved_count) : "0"} />
                <StatusBlock label="达标" value={fulfilledLabel} />
              </div>

              {collectingHint && (
                <div
                  style={{
                    border: "1px solid #bfd7ff",
                    background: "#eff6ff",
                    color: "#175cd3",
                    borderRadius: 8,
                    padding: "10px 12px",
                    fontSize: 13,
                  }}
                >
                  {collectingHint}
                </div>
              )}

              {status?.time_filter_stats && (
                <div
                  style={{
                    border: "1px solid #eaecf0",
                    background: "#fcfcfd",
                    borderRadius: 8,
                    padding: "10px 12px",
                    fontSize: 13,
                    color: "#475467",
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
                    gap: 8,
                  }}
                >
                  <div><span style={{ fontWeight: 600 }}>时间命中</span> {status.time_filter_stats.matched}</div>
                  <div><span style={{ fontWeight: 600 }}>缺少发布时间</span> {status.time_filter_stats.missing_published_at}</div>
                  <div><span style={{ fontWeight: 600 }}>早于开始</span> {status.time_filter_stats.before_start}</div>
                  <div><span style={{ fontWeight: 600 }}>晚于结束</span> {status.time_filter_stats.after_end}</div>
                </div>
              )}

              {status?.gap_reason &&
                (status.state === "completed" || status.state === "failed" || status.state === "stopped") && (
                  <div
                    style={{
                      border: "1px solid #fecdca",
                      background: "#fef3f2",
                      color: "#b42318",
                      borderRadius: 8,
                      padding: "10px 12px",
                      fontSize: 13,
                    }}
                  >
                    {status.gap_reason}
                  </div>
                )}

              <div style={{ display: "flex", gap: 16, flexWrap: "wrap", alignItems: "flex-end" }}>
                <TimeRangePicker
                  timeMode={formState.timeMode}
                  relativeRange={formState.relativeRange}
                  startDate={formState.startDate}
                  endDate={formState.endDate}
                  disabled={disabled}
                  onTimeModeChange={(value) => setFormState((state) => ({ ...state, timeMode: value }))}
                  onRelativeRangeChange={(value) => setFormState((state) => ({ ...state, relativeRange: value }))}
                  onStartDateChange={(value) => setFormState((state) => ({ ...state, startDate: value }))}
                  onEndDateChange={(value) => setFormState((state) => ({ ...state, endDate: value }))}
                />

                <label style={{ display: "grid", gap: 6, minWidth: 140, color: "#475467", fontSize: 13 }}>
                  <span>目标条目数</span>
                  <input
                    type="number"
                    min={1}
                    max={500}
                    value={formState.targetCount}
                    disabled={disabled}
                    onChange={(event) => setFormState((state) => ({ ...state, targetCount: event.target.value }))}
                    style={{
                      border: "1px solid #d0d5dd",
                      borderRadius: 8,
                      padding: "10px 12px",
                      fontSize: 14,
                      color: "#101828",
                      background: "#fff",
                    }}
                  />
                </label>
              </div>
            </div>
          </div>

          {(localError || errorMessage || status?.last_error) && (
            <div
              style={{
                border: "1px solid #fecdca",
                background: "#fef3f2",
                color: "#b42318",
                borderRadius: 8,
                padding: "10px 12px",
                fontSize: 13,
              }}
            >
              {localError ?? errorMessage ?? status?.last_error}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function StatusBlock(props: { label: string; value: string }) {
  return (
    <div style={{ border: "1px solid #eaecf0", borderRadius: 8, padding: 12, background: "#fcfcfd" }}>
      <div style={{ fontSize: 12, color: "#667085", marginBottom: 6 }}>{props.label}</div>
      <div style={{ fontSize: 15, fontWeight: 700, color: "#101828" }}>{props.value}</div>
    </div>
  );
}
