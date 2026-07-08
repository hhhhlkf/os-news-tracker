// frontend/src/components/DiscoveryLogPanel.tsx
import { useState } from "react";
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
  run: "任务",
  fetch: "抓取",
  time_filter: "时间过滤",
  candidate_prefilter: "本地预筛",
  llm_scoring: "LLM筛选",
  queue: "入队",
  process: "处理",
};

type LogView = "all" | "discovery" | "method";

function classifyLog(log: NewsRunLogEntry): LogView {
  return log.stage === "抓方式" || (log.stage === "process" && typeof log.method_id === "number")
    ? "method"
    : "discovery";
}

function compactFields(log: NewsRunLogEntry) {
  const skip = new Set(["id", "ts", "level", "stage", "source", "message"]);
  return Object.entries(log)
    .filter(([key, value]) => !skip.has(key) && value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(" · ");
}

export function DiscoveryLogPanel({ logs }: { logs: NewsRunLogEntry[] }) {
  const [expanded, setExpanded] = useState(true);
  const [view, setView] = useState<LogView>("all");
  const recent = logs
    .filter((log) => view === "all" || classifyLog(log) === view)
    .slice(-80)
    .reverse();
  const discoveryCount = logs.filter((log) => classifyLog(log) === "discovery").length;
  const methodCount = logs.filter((log) => classifyLog(log) === "method").length;
  return (
    <div style={{ background: "#0b1220", borderRadius: 10, padding: "12px 14px",
      fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 12, lineHeight: 1.7, color: "#d0d5dd",
      display: "flex", flexDirection: "column", minHeight: 280, width: "100%", minWidth: 0, height: "100%" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
        <div style={{ display: "grid", gap: 8 }}>
          <span style={{ color: "#f8fafc", fontWeight: 700, fontSize: 13 }}>运行日志</span>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <button type="button" onClick={() => setView("all")} style={viewButton(view === "all")}>
              全部
            </button>
            <button type="button" onClick={() => setView("discovery")} style={viewButton(view === "discovery")}>
              智能探查 {discoveryCount > 0 ? discoveryCount : ""}
            </button>
            <button type="button" onClick={() => setView("method")} style={viewButton(view === "method")}>
              抓方式 {methodCount > 0 ? methodCount : ""}
            </button>
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <span style={{ color: "#98a2b3", fontSize: 11 }}>{logs.length} 条</span>
          <button
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
          </button>
        </div>
      </div>
      <div style={{ overflowY: "auto", flex: 1, maxHeight: expanded ? undefined : "4vh" }}>
        {recent.length === 0 ? (
          <div style={{ color: "#98a2b3" }}>
            {view === "method"
              ? "暂无抓方式日志。开始批量抓取后，这里会显示 DSL 执行、限制应用和入库结果。"
              : view === "discovery"
                ? "暂无智能探查日志。开始探查后，这里会显示各节点进展。"
                : "暂无运行日志。智能探查和爬取方式相关日志都会显示在这里。"}
          </div>
        ) : recent.map((l) => (
          <div key={l.id} style={{ color: l.level === "error" ? "#fda29b" : l.level === "warning" ? "#fedf89" : "#d0d5dd" }}>
            <span style={{ color: "#7cd4fd" }}>{ts(l.ts)}</span>{" "}
            <span style={{ color: "#a6f4c5" }}>[{stageLabels[l.stage] ?? l.stage}]</span>{" "}
            {l.source && <span style={{ color: "#fdb022" }}>{l.source}</span>}{" "}
            <span>{l.message}</span>
            {compactFields(l) && <span style={{ color: "#98a2b3" }}> · {compactFields(l)}</span>}
          </div>
        ))}
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
