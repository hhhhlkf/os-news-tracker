// frontend/src/components/DiscoveryLogPanel.tsx
import { useEffect, useMemo, useRef, useState } from "react";
import type { NewsRunLogEntry } from "../types";

function ts(t: string) {
  const d = new Date(t); return Number.isNaN(d.getTime()) ? "--:--:--" : d.toLocaleTimeString("zh-CN", { hour12: false });
}

const stageLabels: Record<string, string> = {
  "任务": "任务",
  "抓首页": "抓首页",
  "抓网络请求": "抓网络请求",
  "路由": "路由",
  "命名": "命名",
  "探查": "探查",
  "验证URL": "验证URL",
  "写配方": "写配方",
  "审计": "审计",
  "存库": "存库",
  "抓方式": "抓方式",
  context: "准备",
  explore: "探查",
  build: "构建",
  execute: "执行",
  evaluate: "验收",
  repair: "修复",
  package: "封装",
  prepare: "准备",
  connector_sandbox: "沙箱执行",
  output_filter: "结果筛选",
  pipeline: "入库处理",
  complete: "完成",
  failed: "失败",
  run: "任务",
  fetch: "抓取",
  time_filter: "时间过滤",
  candidate_prefilter: "本地预筛",
  llm_scoring: "LLM筛选",
  queue: "入队",
  process: "处理",
};

type LogView = "all" | "discovery" | "method";
type TechnicalLogView = "email" | "github" | "organizer";
type UnifiedLogView = "all" | "discovery" | "method" | "email" | "github" | "organizer";

function classifyLog(log: NewsRunLogEntry): LogView {
  return typeof log.method_id === "number"
    ? "method"
    : "discovery";
}

function classifyUnifiedLog(log: NewsRunLogEntry): Exclude<UnifiedLogView, "all"> {
  if (typeof log.method_id === "number") return "method";
  const provider = String(log.provider ?? log.source_provider ?? "").toLowerCase();
  if (provider.includes("github")) return "github";
  if (provider.includes("organizer") || /整理|建树|归并|价值|发布/.test(log.stage)) return "organizer";
  if (provider.includes("email") || provider.includes("mail") || provider.includes("imap")) return "email";
  return "discovery";
}

function compactFields(log: NewsRunLogEntry) {
  const skip = new Set(["id", "ts", "level", "stage", "phase", "source", "message", "provider", "source_provider", "payload", "run_id", "sequence"]);
  return Object.entries(log)
    .filter(([key, value]) => !skip.has(key) && value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key}=${formatFieldValue(value)}`)
    .join(" · ");
}

function formatFieldValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function durationSeconds(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  return value;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} 秒`;
  const whole = Math.round(seconds);
  const minutes = Math.floor(whole / 60);
  const remainder = whole % 60;
  return remainder > 0 ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分`;
}

function timingLabels(log: NewsRunLogEntry): string[] {
  const payload = log.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return [];
  const record = payload as Record<string, unknown>;
  const eventType = typeof log.event_type === "string" ? log.event_type : "";
  const labels: string[] = [];
  const total = durationSeconds(record.active_execution_elapsed_seconds);
  const completed = durationSeconds(record.completed_phase_elapsed_seconds);
  const current = durationSeconds(record.phase_elapsed_seconds);
  const waiting = durationSeconds(record.queue_wait_seconds);
  const completedPhase = typeof record.completed_phase === "string" ? record.completed_phase : "";
  const timedPhase = typeof record.timed_phase === "string" ? record.timed_phase : "";

  if (waiting !== null) labels.push(`排队等待 ${formatDuration(waiting)}`);
  if (completed !== null && completedPhase) {
    labels.push(`${stageLabels[completedPhase] ?? completedPhase}阶段 ${formatDuration(completed)}`);
  }
  if (current !== null && completed === null) {
    labels.push(`${stageLabels[timedPhase] ?? "本"}阶段 ${formatDuration(current)}`);
  }
  if (total !== null) {
    const terminal = eventType === "artifact_pending_review" || eventType === "wechat_plugin_packaged" || eventType === "run_failed" || eventType === "run_cancelled";
    labels.push(`${terminal ? "总执行" : "累计执行"} ${formatDuration(total)}（不含排队）`);
  }
  return labels;
}

function visibleErrorDetails(log: NewsRunLogEntry): Array<[string, unknown]> {
  const payload = log.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return [];
  const record = payload as Record<string, unknown>;
  return [
    ["错误", record.error_summary],
    ["容器错误", record.container_error_summary],
    ["错误码", record.error_code],
    ["阶段", record.error_stage],
    ["容器退出码", record.returncode],
    ["运行详情", record.stderr_summary],
    ["失败检查", record.failures],
  ].filter((entry): entry is [string, unknown] => entry[1] !== null && entry[1] !== undefined && entry[1] !== "");
}

function technicalLogView(log: NewsRunLogEntry): TechnicalLogView {
  const provider = String(log.provider ?? log.source_provider ?? log.source ?? "").toLowerCase();
  if (provider.includes("github")) return "github";
  if (/整理|建树|归并|筛选|价值|发布/.test(log.stage)) return "organizer";
  return "email";
}

type TechnicalLogLabels = Partial<Record<TechnicalLogView, string>>;

export function DiscoveryLogPanel({
  logs,
  variant = "discovery",
  technicalProviders = ["email", "github", "organizer"],
  technicalLabels,
  liveOnly = false,
}: {
  logs: NewsRunLogEntry[];
  variant?: "discovery" | "discussion" | "technical" | "unified";
  /** Restrict the provider tabs when embedding the shared technical log elsewhere. */
  technicalProviders?: readonly TechnicalLogView[];
  /** Override provider tab labels while retaining the shared log classification. */
  technicalLabels?: TechnicalLogLabels;
  liveOnly?: boolean;
}) {
  const [expanded, setExpanded] = useState(true);
  const [view, setView] = useState<LogView>("all");
  const [technicalView, setTechnicalView] = useState<TechnicalLogView | "all">("all");
  const [unifiedView, setUnifiedView] = useState<UnifiedLogView>("all");
  const viewportRef = useRef<HTMLDivElement>(null);
  const followTailRef = useRef(true);
  const [autoScrollPaused, setAutoScrollPaused] = useState(false);
  const isTechnical = variant === "technical";
  const isUnified = variant === "unified";
  const recent = useMemo(() => logs
    .filter((log) => {
      if (isUnified) return unifiedView === "all" || classifyUnifiedLog(log) === unifiedView;
      if (isTechnical) return technicalView === "all" || technicalLogView(log) === technicalView;
      return view === "all" || classifyLog(log) === view;
    }), [isTechnical, isUnified, logs, technicalView, unifiedView, view]);
  const rowHeight = 28;
  useEffect(() => {
    if (!followTailRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      const viewport = viewportRef.current;
      if (viewport) viewport.scrollTop = viewport.scrollHeight;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [recent.length]);
  const discoveryCount = logs.filter((log) => classifyLog(log) === "discovery").length;
  const methodCount = logs.filter((log) => classifyLog(log) === "method").length;
  const isDiscussion = variant === "discussion";
  const primaryLabel = isDiscussion ? "邮件探查" : "智能探查";
  const label = (provider: TechnicalLogView) => technicalLabels?.[provider] ?? ({ email: "邮件探查", github: "GitHub 探查", organizer: "讨论整理" }[provider]);
  const providerCount = (provider: TechnicalLogView) => logs.filter((log) => technicalLogView(log) === provider).length;
  const unifiedCount = (key: Exclude<UnifiedLogView, "all">) => logs.filter((log) => classifyUnifiedLog(log) === key).length;
  const unifiedTabs: Array<{ id: Exclude<UnifiedLogView, "all">; label: string }> = [
    { id: "discovery", label: "智能探查" },
    { id: "method", label: "抓方式" },
    { id: "email", label: "邮件探查" },
    { id: "github", label: "GitHub 探查" },
    { id: "organizer", label: "讨论整理" },
  ];
  return (
    <div
      className="scrollbar-on-dark"
      style={{ background: "#0b1220", borderRadius: 10, padding: "12px 14px",
      fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 12, lineHeight: 1.7, color: "#d0d5dd",
      display: "flex", flexDirection: "column", minHeight: 280, width: "100%", minWidth: 0, height: "100%" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
        <div style={{ display: "grid", gap: 8 }}>
          <span style={{ color: "#f8fafc", fontWeight: 700, fontSize: 13 }}>{isUnified ? "运行日志" : isTechnical ? "技术探查运行日志" : "运行日志"}</span>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button type="button" onClick={() => {
              if (isUnified) setUnifiedView("all");
              else if (isTechnical) setTechnicalView("all");
              else setView("all");
            }} style={viewButton(isUnified ? unifiedView === "all" : isTechnical ? technicalView === "all" : view === "all")}>
              全部
            </button>
            {isUnified ? unifiedTabs.map((tab) => (
              <button key={tab.id} type="button" onClick={() => setUnifiedView(tab.id)} style={viewButton(unifiedView === tab.id)}>
                {tab.label} {unifiedCount(tab.id) > 0 ? unifiedCount(tab.id) : ""}
              </button>
            )) : isTechnical ? technicalProviders.map((provider) => <button key={provider} type="button" onClick={() => setTechnicalView(provider)} style={viewButton(technicalView === provider)}>
              {label(provider)} {providerCount(provider) > 0 ? providerCount(provider) : ""}
            </button>) : <>
              <button type="button" onClick={() => setView("discovery")} style={viewButton(view === "discovery")}>
                {primaryLabel} {discoveryCount > 0 ? discoveryCount : ""}
              </button>
              {!isDiscussion && <button type="button" onClick={() => setView("method")} style={viewButton(view === "method")}>
              抓方式 {methodCount > 0 ? methodCount : ""}
              </button>}
            </>}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          {autoScrollPaused && <button type="button" onClick={() => {
            followTailRef.current = true;
            setAutoScrollPaused(false);
            const viewport = viewportRef.current;
            if (viewport) viewport.scrollTop = viewport.scrollHeight;
          }} style={viewButton(true)}>回到最新</button>}
          <span style={{ color: "#98a2b3", fontSize: 11 }}>{logs.length} 条</span>
          {!isTechnical && !isUnified && <button
            type="button"
            onClick={() => setExpanded((value) => !value)}
            style={{
              border: "1px solid #344054",
              borderRadius: 6,
              background: expanded ? "#1d2939" : "transparent",
              color: "#d0d5dd",
              cursor: "pointer",
              fontSize: 12,
              padding: "4px 8px",
            }}
          >
            {expanded ? "收起" : "展开"}
          </button>}
        </div>
      </div>
      <div
        ref={viewportRef}
        onScroll={(event) => {
          const viewport = event.currentTarget;
          const atTail = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < rowHeight * 2;
          followTailRef.current = atTail;
          setAutoScrollPaused(!atTail);
        }}
        style={{ overflowY: "auto", flex: 1, maxHeight: expanded ? undefined : "4vh", position: "relative" }}
      >
        {recent.length === 0 ? (
          <div style={{ color: "#98a2b3" }}>
            {isUnified
              ? unifiedView === "method"
                ? "暂无抓方式日志。开始批量抓取后，这里会显示 DSL 执行、限制应用和入库结果。"
                : unifiedView === "email"
                  ? "暂无邮件探查日志。启动收取后，这里会显示邮箱扫描与候选入库进展。"
                  : unifiedView === "github"
                    ? "暂无 GitHub 探查日志。GitHub 同步接入后，Issue 与 Discussion 的同步进展会显示在这里。"
                    : unifiedView === "organizer"
                      ? "暂无讨论整理日志。候选通过筛选后，建树、归并和条目修订进展会显示在这里。"
                      : unifiedView === "discovery"
                        ? "暂无智能探查日志。开始探查后，这里会显示各节点进展。"
                        : "暂无运行日志。智能探查、抓方式和讨论探查都会显示在这里。"
              : isTechnical
              ? technicalView === "github"
                ? "暂无 GitHub 探查日志。GitHub 同步接入后，Issue 与 Discussion 的同步进展会显示在这里。"
                : technicalView === "organizer"
                  ? "暂无讨论整理日志。候选通过筛选后，建树、归并和条目修订进展会显示在这里。"
                  : technicalView === "email"
                    ? "暂无邮件探查日志。启动收取后，这里会显示邮箱扫描与候选入库进展。"
                    : "暂无技术探查日志。邮件、GitHub 与讨论整理会共用这一运行日志。"
              : view === "method"
              ? "暂无抓方式日志。开始批量抓取后，这里会显示 DSL 执行、限制应用和入库结果。"
              : view === "discovery"
                ? isDiscussion
                  ? "暂无邮件探查日志。启动收取后，这里会显示邮箱扫描、建树和讨论整理进展。"
                  : liveOnly
                    ? "从当前时刻开始记录。打开后产生的新日志会出现在这里。"
                    : "暂无智能探查日志。开始探查后，这里会显示各节点进展。"
                : isDiscussion
                  ? "暂无运行日志。启动邮件探查后，这里会显示完整处理过程。"
                  : liveOnly
                    ? "从当前时刻开始记录。不会回放历史日志。"
                    : "暂无运行日志。智能探查和爬取方式相关日志都会显示在这里。"}
          </div>
        ) : <div>
          {recent.map((l) => {
            const errorDetails = visibleErrorDetails(l);
            const durations = timingLabels(l);
            return <div key={l.id} style={{ minHeight: rowHeight, padding: "2px 0", whiteSpace: "pre-wrap", overflowWrap: "anywhere", wordBreak: "break-word", contentVisibility: "auto", color: l.level === "error" ? "#fda29b" : l.level === "warning" ? "#fedf89" : "#d0d5dd" }}>
              <div>
                <span style={{ color: "#7cd4fd" }}>{ts(l.ts)}</span>{" "}
                <span style={{ color: "#a6f4c5" }}>[{stageLabels[String(l.phase ?? l.stage)] ?? String(l.phase ?? l.stage)}]</span>{" "}
                {l.source && <span style={{ color: "#fdb022" }}>{l.source}</span>}{" "}
                <span>{l.message}</span>
                {durations.map((duration) => <span key={duration} style={{ display: "inline-block", marginLeft: 6, padding: "0 5px", borderRadius: 4, background: "#17324d", color: "#9bd8ff", fontSize: 11, lineHeight: "18px" }}>{duration}</span>)}
                {compactFields(l) && <span style={{ color: "#98a2b3" }}> · {compactFields(l)}</span>}
              </div>
              {errorDetails.length > 0 && <div style={{ margin: "4px 0 7px 4px", padding: "7px 9px", borderLeft: "3px solid #f04438", borderRadius: 4, background: "#1d2939", color: "#fecdca" }}>
                {errorDetails.map(([label, value]) => <div key={label}>
                  <strong>{label}：</strong>{formatFieldValue(value)}
                </div>)}
              </div>}
            </div>;
          })}
        </div>}
      </div>
    </div>
  );
}

function viewButton(active: boolean) {
  return {
    border: "1px solid #344054",
    borderRadius: 999,
    background: active ? "#1d2939" : "transparent",
    color: active ? "#f8fafc" : "#98a2b3",
    cursor: "pointer",
    fontSize: 11,
    padding: "3px 9px",
  } as const;
}
