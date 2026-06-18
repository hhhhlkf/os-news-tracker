import { useState } from "react";

import type { NewsRunLogEntry } from "../types";

interface Props {
  logs: NewsRunLogEntry[];
  isLoading?: boolean;
}

const stageLabels: Record<string, string> = {
  run: "任务",
  fetch: "抓取",
  time_filter: "时间过滤",
  candidate_prefilter: "本地预筛",
  llm_scoring: "LLM筛选",
  queue: "入队",
  process: "处理",
};

function formatLogTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--:--";
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}

function compactFields(log: NewsRunLogEntry) {
  const skip = new Set(["id", "ts", "level", "stage", "source", "message"]);
  return Object.entries(log)
    .filter(([key, value]) => !skip.has(key) && value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(" · ");
}

export function NewsRunLogPanel({ logs, isLoading = false }: Props) {
  const [expanded, setExpanded] = useState(false);
  const visibleLogs = logs.slice(-80).reverse();
  return (
    <section
      style={{
        border: "1px solid #d0d5dd",
        borderRadius: 8,
        background: "#0b1220",
        color: "#d0d5dd",
        padding: 14,
        marginBottom: 16,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12, marginBottom: 10 }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "#f8fafc" }}>运行日志</div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{ fontSize: 12, color: "#98a2b3" }}>
            {isLoading ? "加载中" : `${logs.length} 条`}
          </div>
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
      <div
        style={{
          display: "grid",
          gap: 8,
          maxHeight: expanded ? "70vh" : 300,
          overflowY: "auto",
          fontFamily: "JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, monospace",
          fontSize: 12,
          lineHeight: 1.55,
        }}
      >
        {visibleLogs.length === 0 ? (
          <div style={{ color: "#98a2b3" }}>暂无运行日志。启动新闻处理后会显示抓取、筛选、入队和入库过程。</div>
        ) : (
          visibleLogs.map((log) => {
            const fieldText = compactFields(log);
            const color = log.level === "error" ? "#fda29b" : log.level === "warning" ? "#fedf89" : "#d0d5dd";
            return (
              <div key={log.id} style={{ color }}>
                <span style={{ color: "#7cd4fd" }}>{formatLogTime(log.ts)}</span>{" "}
                <span style={{ color: "#a6f4c5" }}>[{stageLabels[log.stage] ?? log.stage}]</span>{" "}
                {log.source && <span style={{ color: "#fdb022" }}>{log.source}</span>}{" "}
                <span>{log.message}</span>
                {fieldText && <span style={{ color: "#98a2b3" }}> · {fieldText}</span>}
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}
