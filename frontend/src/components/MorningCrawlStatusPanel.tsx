import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchMorningCrawlDashboard,
  retryTodayFailedMorningCrawl,
  runMorningCrawlNow,
  stopMorningCrawl,
  updateMorningCrawlConfig,
} from "../morningCrawl/api";
import type { MorningCrawlFrequency, MorningCrawlLookback } from "../morningCrawl/types";
import {
  fetchDiscussionSources,
  fetchGitHubDiscussionRepositories,
  fetchLatestDiscussionPipelineRun,
} from "../discussions/api";

const PANEL: React.CSSProperties = {
  border: "1px solid #e4ebf5",
  borderRadius: 12,
  background: "#fff",
  padding: 16,
};
const PANEL_TITLE: React.CSSProperties = { fontSize: 12, fontWeight: 800, color: "#98a2b3", letterSpacing: 0.4, textTransform: "uppercase" };
const FIELD: React.CSSProperties = {
  border: "1px solid #d0d7e2",
  borderRadius: 8,
  padding: "8px 10px",
  fontSize: 14,
  color: "#101828",
  background: "#fff",
};

const STATUS_META: Record<string, { text: string; bg: string; color: string }> = {
  not_run: { text: "今日未执行", bg: "#f2f4f7", color: "#475467" },
  running: { text: "执行中", bg: "#eff6ff", color: "#175cd3" },
  stopping: { text: "停止中", bg: "#fffaeb", color: "#b54708" },
  cancelled: { text: "已停止", bg: "#f2f4f7", color: "#475467" },
  success: { text: "今日已完成", bg: "#ecfdf3", color: "#067647" },
  partial: { text: "部分成功", bg: "#fffaeb", color: "#b54708" },
  failed: { text: "执行失败", bg: "#fef3f2", color: "#b42318" },
};

const LOOKBACK_LABEL: Record<MorningCrawlLookback, string> = {
  "24h": "近 24 小时",
  "7d": "近 7 天",
  "30d": "近 30 天",
  all: "不限时间",
};

function formatDateTime(value: string | null): string {
  if (!value) return "—";
  const [datePart, timeRaw = ""] = value.split("T");
  if (!datePart) return value;
  const timePart = timeRaw.replace("Z", "").split(".")[0].slice(0, 5);
  return timePart ? `${datePart} ${timePart}` : datePart;
}

export function MorningCrawlStatusPanel() {
  const queryClient = useQueryClient();
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [enabled, setEnabled] = useState(true);
  const [runTime, setRunTime] = useState("07:00");
  const [frequency, setFrequency] = useState<MorningCrawlFrequency>("daily");
  const [intervalHours, setIntervalHours] = useState(1);
  const [lookback, setLookback] = useState<MorningCrawlLookback>("24h");
  const [patrolHours, setPatrolHours] = useState(3);

  const dashboardQuery = useQuery({
    queryKey: ["morning-crawl"],
    queryFn: fetchMorningCrawlDashboard,
    retry: false,
    refetchInterval: (query) => (query.state.data?.is_running ? 2000 : false),
  });
  const dashboard = dashboardQuery.data;
  const discussionSources = useQuery({ queryKey: ["discussion-sources"], queryFn: fetchDiscussionSources, retry: false });
  const discussionRepositories = useQuery({ queryKey: ["discussion-github-repositories"], queryFn: fetchGitHubDiscussionRepositories, retry: false });
  const latestDiscussionRun = useQuery({
    queryKey: ["discussion-pipeline-latest"],
    queryFn: fetchLatestDiscussionPipelineRun,
    retry: false,
    refetchInterval: (query) => query.state.data?.status === "running" || query.state.data?.status === "stopping" ? 2000 : false,
  });

  useEffect(() => {
    if (dashboard && !editing) {
      setEnabled(dashboard.config.enabled);
      setRunTime(dashboard.config.run_time.slice(0, 5));
      setFrequency(dashboard.config.frequency);
      setIntervalHours(dashboard.config.interval_hours);
      setLookback(dashboard.config.lookback_window);
      setPatrolHours(dashboard.config.patrol_interval_hours);
    }
  }, [dashboard, editing]);


  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["morning-crawl"] });

  const saveMutation = useMutation({
    mutationFn: async () =>
      updateMorningCrawlConfig({
        enabled,
        run_time: runTime,
        frequency,
        interval_hours: intervalHours,
        lookback_window: lookback,
        patrol_interval_hours: patrolHours,
      }),
    onSuccess: () => {
      setStatusMessage("定时抓取配置已保存。");
      setEditing(false);
      invalidate();
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "保存失败"),
  });

  const runNowMutation = useMutation({
    mutationFn: async () => runMorningCrawlNow(),
    onSuccess: (run) => {
      setStatusMessage(run.status === "running" ? "已开始执行定时抓取，进度将自动刷新。" : `定时抓取已触发（${run.status}）。`);
      invalidate();
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "触发失败"),
  });

  const retryTodayMutation = useMutation({
    mutationFn: async () => retryTodayFailedMorningCrawl(),
    onSuccess: () => {
      setStatusMessage(`已开始重试今日未成功的 ${dashboard?.retryable_method_count ?? 0} 个抓取方式。`);
      invalidate();
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "重试失败"),
  });

  const stopMutation = useMutation({
    mutationFn: async () => stopMorningCrawl(),
    onSuccess: () => {
      setStatusMessage("已发出停止请求，正在停止当前爬取（当前方式跑完后终止后续）。");
      invalidate();
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "停止失败"),
  });

  const toggleEnabledMutation = useMutation({
    mutationFn: async (next: boolean) => updateMorningCrawlConfig({ enabled: next }),
    onSuccess: (_data, next) => {
      setStatusMessage(
        next
          ? "已开启定时抓取，到点将自动执行（需 ENABLE_MORNING_CRAWL_SCHEDULER 开启）。"
          : "已关闭定时抓取，将不再到点自动执行（不影响手动「立即执行」）。",
      );
      setEditing(false);
      invalidate();
    },
    onError: (error) => setStatusMessage(error instanceof Error ? error.message : "操作失败"),
  });

  const status = dashboard?.today_status ?? "not_run";
  const statusMeta = STATUS_META[status] ?? STATUS_META.not_run;
  const running = dashboard?.is_running ?? false;
  const scheduleEnabled = dashboard?.config.enabled ?? false;
  const retryableMethodCount = dashboard?.retryable_method_count ?? 0;
  const enabledMailSources = (discussionSources.data ?? []).filter((source) => source.enabled && source.review_status === "approved").length;
  const enabledGitHubRepositories = (discussionRepositories.data ?? []).filter((repository) => repository.enabled).length;
  const technicalRun = latestDiscussionRun.data;
  const technicalRunLabel = technicalRun
    ? technicalRun.status === "running" || technicalRun.status === "stopping"
      ? "正在运行"
      : technicalRun.status === "succeeded"
        ? "最近成功"
        : technicalRun.status === "partial"
          ? "最近部分成功"
          : technicalRun.status === "failed"
            ? "最近失败"
            : technicalRun.status === "cancelled"
              ? "最近已停止"
              : technicalRun.status
    : "暂无运行记录";
  const latestDiscussionInsertCount = technicalRun
    ? (technicalRun.result?.received ?? []).reduce((total, result) => total + (result.inserted ?? 0), 0)
      + (technicalRun.result?.github ?? []).reduce(
        (total, result) => total
          + (result.issues?.roots ?? 0)
          + (result.issues?.comments ?? 0)
          + (result.discussions?.roots ?? 0)
          + (result.discussions?.comments ?? 0)
          + (result.discussions?.replies ?? 0),
        0,
      )
    : null;

  return (
    <div style={{ display: "grid", gap: 12 }}>
      {statusMessage && (
        <div style={{ display: "flex", alignItems: "flex-start", gap: 8, fontSize: 13, color: "#175cd3", background: "#eff6ff", border: "1px solid #d3e3fb", borderRadius: 8, padding: "8px 12px" }}>
          <span style={{ flex: 1 }}>{statusMessage}</span>
          <button
            onClick={() => setStatusMessage(null)}
            aria-label="关闭提示"
            title="关闭"
            style={{ border: "none", background: "transparent", color: "inherit", cursor: "pointer", fontSize: 15, lineHeight: 1, padding: 0, opacity: 0.7 }}
          >
            ×
          </button>
        </div>
      )}

      {/* 顶部状态指标 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0,1fr))", gap: 10 }}>
        <div style={{ ...PANEL, padding: "12px 14px" }}>
          <div style={{ ...PANEL_TITLE, marginBottom: 6 }}>今日状态</div>
          <span style={{ display: "inline-block", fontSize: 16, fontWeight: 800, borderRadius: 999, padding: "3px 10px", background: statusMeta.bg, color: statusMeta.color }}>
            {statusMeta.text}
          </span>
          <div style={{ fontSize: 12, color: "#667085", marginTop: 6 }}>
            {dashboard?.today_run
              ? `方式 ${dashboard.today_run.success_methods}/${dashboard.today_run.total_methods}${retryableMethodCount ? ` · 可重试 ${retryableMethodCount}` : ""}`
              : "今日尚无执行记录"}
          </div>
        </div>
        <div style={{ ...PANEL, padding: "12px 14px" }}>
          <div style={{ ...PANEL_TITLE, marginBottom: 6 }}>关联爬取方式</div>
          <div style={{ fontSize: 16, fontWeight: 800, color: "#101828" }}>{dashboard?.active_method_count ?? 0}</div>
          <div style={{ fontSize: 12, color: "#667085" }}>启用中 discovery methods</div>
        </div>
        <div style={{ ...PANEL, padding: "12px 14px" }}>
          <div style={{ ...PANEL_TITLE, marginBottom: 6 }}>今日入库</div>
          <div style={{ fontSize: 16, fontWeight: 800, color: "#101828" }}>{dashboard?.today_run?.stored_count ?? 0}</div>
          <div style={{ fontSize: 12, color: "#667085" }}>今日新增入库条数</div>
        </div>
        <div style={{ ...PANEL, padding: "12px 14px" }}>
          <div style={{ ...PANEL_TITLE, marginBottom: 6 }}>下次执行</div>
          <div style={{ fontSize: 16, fontWeight: 800, color: "#101828" }}>{formatDateTime(dashboard?.config.next_run_at ?? null)}</div>
          <div style={{ fontSize: 12, color: "#667085" }}>北京时间 · {dashboard?.config.enabled ? "已启用" : "已停用"}</div>
        </div>
        <TechnicalStatusCard label="启用邮件来源探查" value={`${enabledMailSources}`} hint="已审核且启用的邮件来源" />
        <TechnicalStatusCard label="启用 GitHub 仓库" value={`${enabledGitHubRepositories}`} hint="同步 Issue、Discussion 与评论" />
        <TechnicalStatusCard
          label="最近讨论新增"
          value={latestDiscussionInsertCount === null ? "—" : `${latestDiscussionInsertCount} 条`}
          hint={technicalRun?.started_at ? `邮件与 GitHub 合计 · ${formatDateTime(technicalRun.started_at)}` : "暂无技术讨论同步记录"}
        />
        <TechnicalStatusCard label="最近技术讨论任务" value={technicalRunLabel} hint={technicalRun?.started_at ? formatDateTime(technicalRun.started_at) : "邮件与 GitHub 共用一轮任务"} />
      </div>

      {/* 定时抓取配置表单 */}
      <section style={PANEL}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
          <div style={PANEL_TITLE}>定时抓取配置</div>
          {editing ? (
            <div style={{ display: "flex", gap: 8 }}>
              <button
                onClick={() => saveMutation.mutate()}
                disabled={saveMutation.isPending}
                style={{ border: "none", borderRadius: 8, padding: "6px 14px", fontSize: 12, fontWeight: 700, color: "#fff", background: "#175cd3", cursor: "pointer" }}
              >
                保存
              </button>
              <button
                onClick={() => { setEditing(false); setStatusMessage(null); }}
                style={{ border: "1px solid #d0d7e2", borderRadius: 8, padding: "6px 14px", fontSize: 12, fontWeight: 700, color: "#475467", background: "#fff", cursor: "pointer" }}
              >
                取消
              </button>
            </div>
          ) : (
            <button
              onClick={() => setEditing(true)}
              style={{ border: "1px solid #d0d7e2", borderRadius: 8, padding: "6px 14px", fontSize: 12, fontWeight: 700, color: "#344054", background: "#fff", cursor: "pointer" }}
            >
              编辑配置
            </button>
          )}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 12 }}>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>{frequency === "hourly" ? "小时锚点 (北京时间)" : "执行时间 (北京时间)"}</span>
            <input type="time" value={runTime} disabled={!editing} onChange={(e) => setRunTime(e.target.value)} style={{ ...FIELD, opacity: editing ? 1 : 0.7 }} />
          </label>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>频率</span>
            <select value={frequency} disabled={!editing} onChange={(e) => setFrequency(e.target.value as MorningCrawlFrequency)} style={{ ...FIELD, opacity: editing ? 1 : 0.7 }}>
              <option value="hourly">每 N 小时</option>
              <option value="daily">每日</option>
              <option value="weekdays">每日（仅工作日）</option>
              <option value="weekly">每周</option>
            </select>
            {frequency === "hourly" && <span style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 11, color: "#667085" }}>每 <input type="number" min={1} max={24} value={intervalHours} disabled={!editing} onChange={(e) => setIntervalHours(Number(e.target.value))} style={{ ...FIELD, width: 48, padding: "4px 6px", opacity: editing ? 1 : 0.7 }} /> 小时</span>}
          </label>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>抓取时间窗口</span>
            <select value={lookback} disabled={!editing} onChange={(e) => setLookback(e.target.value as MorningCrawlLookback)} style={{ ...FIELD, opacity: editing ? 1 : 0.7 }}>
              {(Object.keys(LOOKBACK_LABEL) as MorningCrawlLookback[]).map((k) => (
                <option key={k} value={k}>{LOOKBACK_LABEL[k]}</option>
              ))}
            </select>
          </label>
          <label style={{ display: "grid", gap: 4 }}>
            <span style={{ fontSize: 12, fontWeight: 700, color: "#475467" }}>巡检间隔 (小时)</span>
            <input type="number" min={1} max={24} value={patrolHours} disabled={!editing} onChange={(e) => setPatrolHours(Number(e.target.value))} style={{ ...FIELD, opacity: editing ? 1 : 0.7 }} />
          </label>
        </div>

        <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 12, fontSize: 13, color: "#344054", cursor: editing ? "pointer" : "default" }}>
          <input type="checkbox" checked={enabled} disabled={!editing} onChange={(e) => setEnabled(e.target.checked)} />
          启用系统定时抓取（到点自动执行；需开启 ENABLE_MORNING_CRAWL_SCHEDULER）
        </label>
      </section>

      {/* 规则解释 */}
      <section style={{ ...PANEL, background: "#f8fafc" }}>
        <div style={{ ...PANEL_TITLE, marginBottom: 8 }}>规则说明</div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 22, fontSize: 12, color: "#475467", lineHeight: 1.7 }}>
          <div><b style={{ color: "#344054" }}>网页爬取</b><div>仅运行 active 爬取方式并正常入库。单项失败不中断整轮；失败或部分成功由巡检补跑。</div><div>工作日模式周末跳过；按小时模式从锚点循环执行。</div></div>
          <div><b style={{ color: "#344054" }}>技术讨论</b><div>邮件与 GitHub 由独立任务统一同步、建树和整理，不混入网页爬取时间线。</div><div>详情在技术探查运行日志查看；所有时间均按北京时间展示。</div></div>
        </div>
      </section>

      <div style={{ display: "flex", gap: 10 }}>
        <button
          onClick={() => runNowMutation.mutate()}
          disabled={runNowMutation.isPending || running}
          style={{
            flex: 1,
            border: "none",
            borderRadius: 10,
            padding: "12px 16px",
            fontSize: 14,
            fontWeight: 800,
            color: "#fff",
            background: running ? "#98a2b3" : "#175cd3",
            cursor: running ? "not-allowed" : "pointer",
          }}
        >
          {running ? "定时抓取执行中…" : "立即执行爬取方式"}
        </button>
        <button
          onClick={() => retryTodayMutation.mutate()}
          disabled={retryTodayMutation.isPending || running || retryableMethodCount === 0}
          title={retryableMethodCount ? "仅重试今日未成功或未执行的网页、论文抓取方式" : "今日没有可重试的抓取方式"}
          style={{
            flex: 1,
            border: retryableMethodCount ? "1px solid #fecdca" : "1px solid #d0d5dd",
            borderRadius: 10,
            padding: "12px 16px",
            fontSize: 14,
            fontWeight: 800,
            color: retryableMethodCount ? "#b42318" : "#98a2b3",
            background: "#fff",
            cursor: retryTodayMutation.isPending || running || retryableMethodCount === 0 ? "not-allowed" : "pointer",
          }}
        >
          {retryTodayMutation.isPending ? "正在启动重试…" : `重试今日未成功链接${retryableMethodCount ? ` (${retryableMethodCount})` : ""}`}
        </button>
        <button
          onClick={() => stopMutation.mutate()}
          disabled={stopMutation.isPending || !running}
          title="停止所有正在进行的爬取（当前方式执行完后终止后续）"
          style={{
            flex: 1,
            border: running ? "none" : "1px solid #f0c4c0",
            borderRadius: 10,
            padding: "12px 16px",
            fontSize: 14,
            fontWeight: 800,
            color: running ? "#fff" : "#d0908b",
            background: running ? "#d92d20" : "#fff",
            cursor: running ? "pointer" : "not-allowed",
          }}
        >
          {stopMutation.isPending ? "正在停止…" : "停止所有爬取"}
        </button>
        <button
          onClick={() => toggleEnabledMutation.mutate(!scheduleEnabled)}
          disabled={toggleEnabledMutation.isPending || !dashboard}
          title={scheduleEnabled ? "关闭定时抓取：不再到点自动执行" : "开启定时抓取：到点自动执行"}
          style={{
            flex: 1,
            borderRadius: 10,
            padding: "12px 16px",
            fontSize: 14,
            fontWeight: 800,
            cursor: toggleEnabledMutation.isPending || !dashboard ? "not-allowed" : "pointer",
            border: scheduleEnabled ? "1px solid #f0c4c0" : "none",
            color: scheduleEnabled ? "#b42318" : "#fff",
            background: scheduleEnabled ? "#fff" : "#067647",
          }}
        >
          {toggleEnabledMutation.isPending
            ? "处理中…"
            : scheduleEnabled
              ? "关闭定时抓取"
              : "开启定时抓取"}
        </button>
      </div>
    </div>
  );
}

function TechnicalStatusCard({
  label,
  value,
  hint,
  actionLabel,
  actionDisabled = false,
  onAction,
}: {
  label: string;
  value: string;
  hint: string;
  actionLabel?: string;
  actionDisabled?: boolean;
  onAction?: () => void;
}) {
  return <div style={{ ...PANEL, padding: "12px 14px", minWidth: 0 }}><div style={{ ...PANEL_TITLE, marginBottom: 6 }}>{label}</div><div style={{ fontSize: 16, fontWeight: 800, color: "#101828", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{value}</div><div style={{ fontSize: 12, color: "#667085", marginTop: 6, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{hint}</div>{actionLabel && onAction && <button onClick={onAction} disabled={actionDisabled} style={{ marginTop: 8, border: "1px solid #d0d7e2", borderRadius: 6, padding: "3px 8px", fontSize: 11, fontWeight: 700, color: "#344054", background: "#fff", cursor: actionDisabled ? "not-allowed" : "pointer", opacity: actionDisabled ? 0.6 : 1 }}>{actionDisabled ? "处理中…" : actionLabel}</button>}</div>;
}
