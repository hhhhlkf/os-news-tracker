import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { fetchMorningCrawlRunDetail, fetchMorningCrawlRuns } from "../morningCrawl/api";
import { useMorningCrawlLogs } from "../hooks/useMorningCrawlLogs";

const RUNNING_STATUSES = new Set(["running", "stopping"]);

const METHOD_STATUS_META: Record<string, { text: string; bg: string; color: string }> = {
  running: { text: "执行中", bg: "#eff6ff", color: "#175cd3" },
  ok: { text: "成功", bg: "#ecfdf3", color: "#067647" },
  empty: { text: "无新增", bg: "#f2f4f7", color: "#475467" },
  failed: { text: "失败", bg: "#fef3f2", color: "#b42318" },
};

const RUN_STATUS_TEXT: Record<string, string> = {
  running: "执行中",
  stopping: "停止中",
  cancelled: "已停止",
  success: "已完成",
  partial: "部分成功",
  failed: "失败",
};

function fmtTime(value: string | null): string {
  if (!value) return "—";
  const [, timeRaw = ""] = value.split("T");
  const t = timeRaw.replace("Z", "").split(".")[0];
  return t || value;
}

function fmtRunLabel(run: { id: number; run_date: string | null; status: string; started_at: string | null }): string {
  const date = run.run_date ?? (run.started_at ? run.started_at.split("T")[0] : "");
  const time = run.started_at ? fmtTime(run.started_at) : "";
  const status = RUN_STATUS_TEXT[run.status] ?? run.status;
  return `#${run.id} · ${date} ${time} · ${status}`;
}

const LOG_PANE: React.CSSProperties = {
  background: "#0d1117",
  border: "1px solid #1f2733",
  borderRadius: 10,
  padding: 12,
  fontFamily: "var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace)",
  fontSize: 12.5,
  lineHeight: 1.6,
  color: "#c9d1d9",
  overflowY: "auto",
  flex: 1,
  minHeight: 0,
};
const PANE_TITLE: React.CSSProperties = { fontSize: 12, fontWeight: 800, color: "#98a2b3", letterSpacing: 0.4, textTransform: "uppercase", marginBottom: 8 };

export function MorningCrawlTimelinePanel() {
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);

  const runsQuery = useQuery({
    queryKey: ["morning-crawl-runs"],
    queryFn: () => fetchMorningCrawlRuns(30),
    retry: false,
    refetchInterval: 4000,
  });

  useEffect(() => {
    if (selectedRunId == null && runsQuery.data?.default_run_id != null) {
      setSelectedRunId(runsQuery.data.default_run_id);
    }
  }, [runsQuery.data?.default_run_id, selectedRunId]);

  const detailQuery = useQuery({
    queryKey: ["morning-crawl-run", selectedRunId],
    queryFn: () => fetchMorningCrawlRunDetail(selectedRunId as number),
    enabled: selectedRunId != null,
    retry: false,
    refetchInterval: (query) => (query.state.data && RUNNING_STATUSES.has(query.state.data.run.status) ? 2000 : false),
  });

  const detail = detailQuery.data;
  const runIsActive = detail ? RUNNING_STATUSES.has(detail.run.status) : false;
  const liveLogs = useMorningCrawlLogs(true);
  const failures = useMemo(() => (detail?.methods ?? []).filter((m) => m.status === "failed"), [detail]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, height: "100%", minHeight: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>查看运行</span>
        <select
          value={selectedRunId ?? ""}
          onChange={(e) => setSelectedRunId(e.target.value ? Number(e.target.value) : null)}
          style={{ border: "1px solid #d0d7e2", borderRadius: 8, padding: "6px 10px", fontSize: 13, color: "#101828", background: "#fff", minWidth: 260 }}
        >
          {(runsQuery.data?.runs.length ?? 0) === 0 && <option value="">暂无运行记录</option>}
          {runsQuery.data?.runs.map((r) => (
            <option key={r.id} value={r.id}>{fmtRunLabel(r)}</option>
          ))}
        </select>
        {detail && (
          <span style={{ fontSize: 12, color: "#667085" }}>
            方式 {detail.run.success_methods}/{detail.run.total_methods} · 失败 {detail.run.failed_methods} · 入库 {detail.run.stored_count}
          </span>
        )}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1.2fr) minmax(0,0.8fr)", gap: 12, flex: 1, minHeight: 0 }}>
        {/* 今日时间线 */}
        <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
          <div style={PANE_TITLE}>今日时间线 {runIsActive && <span style={{ color: "#175cd3" }}>· 实时</span>}</div>
          <div className="scrollbar-on-dark" style={LOG_PANE}>
            {/* 每条方式执行明细 */}
            {(detail?.methods ?? []).map((m) => {
              const meta = METHOD_STATUS_META[m.status] ?? METHOD_STATUS_META.empty;
              return (
                <div key={m.id} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "2px 0" }}>
                  <span style={{ color: "#6e7681", minWidth: 68 }}>{fmtTime(m.finished_at ?? m.started_at)}</span>
                  <span style={{ display: "inline-block", fontSize: 11, fontWeight: 700, borderRadius: 999, padding: "0 8px", background: meta.bg, color: meta.color }}>{meta.text}</span>
                  <span style={{ color: "#c9d1d9", flex: 1 }}>
                    {m.domain ?? `method#${m.method_id}`}
                    <span style={{ color: "#6e7681" }}> · 抓 {m.discovered_count} / 入库 {m.stored_count}</span>
                    {m.error_message && <span style={{ color: "#ff7b72" }}> · {m.error_message}</span>}
                  </span>
                </div>
              );
            })}

            {/* 实时日志 tail */}
            {liveLogs.length > 0 && (
              <div style={{ borderTop: "1px dashed #30363d", marginTop: 8, paddingTop: 8 }}>
                {liveLogs.map((log) => (
                  <div key={log.id} style={{ display: "flex", gap: 8, alignItems: "baseline", padding: "1px 0" }}>
                    <span style={{ color: "#6e7681", minWidth: 68 }}>{fmtTime(log.ts)}</span>
                    <span style={{ color: log.level === "error" ? "#ff7b72" : log.level === "warning" || log.level === "warn" ? "#e3b341" : "#8b949e" }}>
                      {typeof log.source === "string" && log.source ? `[${log.source}] ` : ""}
                    </span>
                    <span style={{ color: "#c9d1d9", flex: 1 }}>{log.message}</span>
                  </div>
                ))}
              </div>
            )}

            {(detail?.methods?.length ?? 0) === 0 && liveLogs.length === 0 && (
              <div style={{ color: "#6e7681" }}>暂无时间线数据{runIsActive ? "，等待抓取输出…" : ""}。</div>
            )}
          </div>
        </div>

        {/* 失败方式 */}
        <div style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
          <div style={PANE_TITLE}>失败方式 {failures.length > 0 && <span style={{ color: "#b42318" }}>· {failures.length}</span>}</div>
          <div className="scrollbar-on-dark" style={LOG_PANE}>
            {failures.length === 0 ? (
              <div style={{ color: "#6e7681" }}>本次运行没有失败的爬取方式。</div>
            ) : (
              failures.map((m) => (
                <div key={m.id} style={{ padding: "6px 0", borderBottom: "1px solid #21262d" }}>
                  <div style={{ color: "#ff7b72", fontWeight: 700 }}>{m.domain ?? `method#${m.method_id}`}</div>
                  <div style={{ color: "#8b949e", fontSize: 11 }}>method#{m.method_id} · {fmtTime(m.started_at)} → {fmtTime(m.finished_at)}</div>
                  {m.error_message && <div style={{ color: "#c9d1d9", marginTop: 2 }}>{m.error_message}</div>}
                </div>
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
