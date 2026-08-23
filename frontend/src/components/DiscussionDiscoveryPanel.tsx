import { useEffect, useMemo, useState, type FormEvent, type ReactNode, type UIEvent } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import { DiscoveryLogPanel } from "./DiscoveryLogPanel";
import { buildManualNewsRunRequest, type NewsRunFormState } from "./runLimits";
import {
  createDiscussionConnection,
  createDiscussionSource,
  createGitHubDiscussionRepository,
  fetchDiscussionGroupCounts,
  fetchDiscussionConnections,
  fetchDiscussionGroups,
  fetchDiscussionPipelineEvents,
  fetchLatestDiscussionPipelineRun,
  fetchGitHubTokenStatus,
  stopDiscussionPipeline,
  type DiscussionPipelineRunState,
  type DiscussionGroupStatus,
  type DiscussionGroupSummary,
} from "../discussions/api";

const initialConnection = {
  name: "专用 Gmail 讨论邮箱", host: "imap.googlemail.com", port: "993", folder: "INBOX",
  username_env_key: "DISCUSSION_IMAP_USERNAME", password_env_key: "DISCUSSION_IMAP_PASSWORD",
};

const DISCUSSION_PANEL_STORAGE_KEY = "os-news-tracker.discussion-panel";
type ResetAtByChannel = Partial<Record<TechnicalDiscoveryTab, number>>;

function readDiscussionPanelResetAt(): ResetAtByChannel {
  if (typeof window === "undefined" || !window.sessionStorage) return {};
  const raw = window.sessionStorage.getItem(DISCUSSION_PANEL_STORAGE_KEY);
  if (!raw) return {};
  try {
    const value = JSON.parse(raw) as Partial<Record<TechnicalDiscoveryTab, unknown>>;
    return Object.fromEntries(
      (["email", "github"] as const)
        .filter((channel) => Number.isFinite(Number(value[channel])) && Number(value[channel]) > 0)
        .map((channel) => [channel, Number(value[channel])]),
    ) as ResetAtByChannel;
  } catch {
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? { email: value } : {};
  }
}

function writeDiscussionPanelResetAt(value: ResetAtByChannel): void {
  if (typeof window === "undefined" || !window.sessionStorage) return;
  window.sessionStorage.setItem(DISCUSSION_PANEL_STORAGE_KEY, JSON.stringify(value));
}

const statusMeta = {
  running: { text: "收取整理中", color: "#175cd3", background: "#eff6ff", border: "#b9d4ff" },
  stopping: { text: "停止中", color: "#b54708", background: "#fffaeb", border: "#fedf89" },
  cancelled: { text: "已停止", color: "#475467", background: "#f2f4f7", border: "#d0d5dd" },
  succeeded: { text: "收取完成", color: "#059669", background: "#ecfdf3", border: "#a3e0c4" },
  partial: { text: "部分成功", color: "#b54708", background: "#fffaeb", border: "#fedf89" },
  failed: { text: "最近失败", color: "#b42318", background: "#fef2f2", border: "#fca5a5" },
};

type RunView = "overview" | "all" | "published" | "waiting";
export type TechnicalDiscoveryTab = "email" | "github";

function pipelineRunChannel(run: DiscussionPipelineRunState | null | undefined): TechnicalDiscoveryTab | null {
  const hasMailSources = run?.source_ids !== null && run?.source_ids !== undefined;
  const hasGitHubRepositories = run?.github_repository_ids !== null && run?.github_repository_ids !== undefined;
  if (hasMailSources && !hasGitHubRepositories) return "email";
  if (hasGitHubRepositories && !hasMailSources) return "github";
  return null;
}

function parsePipelineRunTimestamp(value: string): number {
  // PostgreSQL timestamps from older pipeline rows omit their UTC offset.
  // Treat those API values as UTC instead of letting each browser interpret
  // them in its local timezone; otherwise an Asia/Shanghai browser places a
  // fresh run eight hours before a session reset boundary.
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  return Date.parse(normalized);
}

type DiscussionDiscoveryPanelProps = {
  runLimitState: NewsRunFormState;
  runId: string | null;
  technicalTab: TechnicalDiscoveryTab;
  onTechnicalTabChange: (tab: TechnicalDiscoveryTab) => void;
  /** 管理员才可变更技术讨论的持久配置。 */
  canManage: boolean;
};

export function DiscussionDiscoveryPanel(props: DiscussionDiscoveryPanelProps) {
  return <DiscussionDiscoveryPanelContent {...props} />;
}

function DiscussionDiscoveryPanelContent({ runLimitState, runId, technicalTab, onTechnicalTabChange, canManage }: DiscussionDiscoveryPanelProps) {
  const queryClient = useQueryClient();
  const [connection, setConnection] = useState(initialConnection);
  const [sourceName, setSourceName] = useState("");
  const [ruleType, setRuleType] = useState("list_id");
  const [ruleValue, setRuleValue] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(true);
  const [runView, setRunView] = useState<RunView>("overview");
  const [resetAt, setResetAt] = useState<ResetAtByChannel>(() => readDiscussionPanelResetAt());
  const [stopBusy, setStopBusy] = useState(false);

  const latestRunQuery = useQuery({
    queryKey: ["discussion-latest-run"],
    queryFn: fetchLatestDiscussionPipelineRun,
    retry: false,
    refetchInterval: (query) => ["running", "stopping"].includes(query.state.data?.status ?? "") ? 1000 : false,
  });

  useEffect(() => {
    if (!notice && !error) return;
    const timeoutId = window.setTimeout(() => { setNotice(null); setError(null); }, 5000);
    return () => window.clearTimeout(timeoutId);
  }, [notice, error]);

  const connections = useQuery({ queryKey: ["discussion-connections"], queryFn: fetchDiscussionConnections, retry: false });
  const latestRun = latestRunQuery.data;
  const latestRunChannel = pipelineRunChannel(latestRun);
  // A task started from either library may not have gone through this panel's
  // local state.  Follow the latest task only when it belongs to the selected
  // channel; otherwise retain the explicitly selected task rather than
  // rendering GitHub events in the mail tab (or vice versa).
  const visibleRunId = latestRunChannel === technicalTab ? latestRun?.id ?? runId : runId;
  const runStream = useQuery({
    queryKey: ["discussion-run-events", visibleRunId],
    queryFn: () => fetchDiscussionPipelineEvents(visibleRunId as string),
    enabled: Boolean(visibleRunId),
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.run.status;
      return status === "running" || status === "stopping" ? 1000 : false;
    },
  });
  const groupStatus: DiscussionGroupStatus = runView === "published" ? "published" : runView === "waiting" ? "waiting" : "all";
  const groupCounts = useQuery({
    queryKey: ["discussion-group-counts", "mail"],
    queryFn: () => fetchDiscussionGroupCounts("mail"),
    retry: false,
    refetchInterval: () => runStream.data?.run.status === "running" || runStream.data?.run.status === "stopping" ? 2000 : false,
  });
  const groupPages = useInfiniteQuery({
    queryKey: ["discussion-groups", "mail", groupStatus],
    queryFn: ({ pageParam }) => fetchDiscussionGroups(groupStatus, pageParam, "mail"),
    initialPageParam: 0,
    getNextPageParam: (page) => page.next_offset ?? undefined,
    retry: false,
  });

  const latestRunData = runStream.data?.run
    ?? (visibleRunId === latestRun?.id ? latestRun : undefined);
  const latestStartedAt = latestRunData ? parsePipelineRunTimestamp(latestRunData.started_at) : Number.NaN;
  const selectedRunChannel = pipelineRunChannel(latestRunData);
  const resetAtForRun = selectedRunChannel ? resetAt[selectedRunChannel] : undefined;
  const run = latestRunData && (resetAtForRun == null || (Number.isFinite(latestStartedAt) && latestStartedAt > resetAtForRun))
    ? latestRunData
    : undefined;
  const result = run?.result;
  const receipt = result?.received ?? [];
  const totals = receipt.reduce((summary, item) => ({
    headers: summary.headers + (item.headers ?? 0),
    matched: summary.matched + (item.matched ?? 0),
    unknown: summary.unknown + (item.unknown ?? 0),
    inserted: summary.inserted + (item.inserted ?? 0),
  }), { headers: 0, matched: 0, unknown: 0, inserted: 0 });
  const counts = groupCounts.data ?? { all: 0, published: 0, waiting: 0 };
  const visibleGroups = groupPages.data?.pages.flatMap((page) => page.groups) ?? [];
  const badge = run ? statusMeta[run.status] : null;
  const logs = useMemo(() => {
    if (!run) return [];
    // The creation response already contains the queued event.  Render it
    // while the independent event-stream request is in flight, so a run
    // started from a method library never has a blank status panel.
    const events = runStream.data?.events
      ?? (visibleRunId === latestRun?.id ? latestRun.events : []);
    return events.map((event, index) => ({
      id: "sequence" in event && typeof event.sequence === "number" ? event.sequence : index + 1,
      ts: event.at,
      level: event.level === "success" ? "info" : event.level,
      stage: event.stage,
      source: event.source ?? null,
      message: event.message,
      provider: event.provider ?? "organizer",
    }));
  }, [run, runStream.data?.events, visibleRunId, latestRun]);
  const runChannel = run?.github_repository_ids !== null && run?.source_ids === null ? "GitHub" : run?.source_ids !== null && run?.github_repository_ids === null ? "邮件" : run?.result?.github?.length && !receipt.length ? "GitHub" : run?.result?.github?.length ? "技术探查" : "邮件";
  const mailRunActive = Boolean(run && ["running", "stopping"].includes(run.status) && run.source_ids !== null && run.github_repository_ids === null);
  const githubRunActive = Boolean(run && ["running", "stopping"].includes(run.status) && run.github_repository_ids !== null && run.source_ids === null);

  async function refresh(): Promise<void> {
    await Promise.all([connections.refetch(), runStream.refetch(), groupCounts.refetch(), groupPages.refetch()]);
  }

  function reportFailure(value: unknown): void {
    setError(value instanceof ApiError ? value.message : "操作失败，请稍后重试");
  }

  function resetPanelState(): void {
    const now = Date.now();
    const nextResetAt = { ...resetAt, [technicalTab]: now };
    writeDiscussionPanelResetAt(nextResetAt);
    setResetAt(nextResetAt);
    if (technicalTab === "email") {
      setConnection(initialConnection);
      setSourceName("");
      setRuleType("list_id");
      setRuleValue("");
    }
    setNotice(`${technicalTab === "email" ? "邮件" : "GitHub"}探查状态已重置；历史讨论树会保留，下一次探查会在这里显示新的进展。`);
    setError(null);
    setExpanded(true);
    setRunView("overview");
  }

  async function stopCurrentRun(): Promise<void> {
    if (!run) return;
    setError(null); setNotice(null); setStopBusy(true);
    try {
      await stopDiscussionPipeline(run.id);
      setNotice(`已请求停止${technicalTab === "email" ? "邮件" : "GitHub"}探查；当前网络或模型调用结束后将停止后续处理。`);
      await refresh();
    } catch (value) { reportFailure(value); } finally { setStopBusy(false); }
  }

  async function saveConnection(event: FormEvent): Promise<void> {
    event.preventDefault(); setError(null); setNotice(null);
    try {
      await createDiscussionConnection({
        ...connection,
        name: connection.name.trim(), host: connection.host.trim(), folder: connection.folder.trim(),
        port: Number(connection.port), username_env_key: connection.username_env_key.trim(), password_env_key: connection.password_env_key.trim(), enabled: true,
      });
      setNotice("邮箱连接已保存并启用。");
      await refresh();
    } catch (value) { reportFailure(value); }
  }

  async function addSource(event: FormEvent): Promise<void> {
    event.preventDefault(); setError(null); setNotice(null);
    try {
      await createDiscussionSource({ name: sourceName.trim(), rule_type: ruleType, match_value: ruleValue.trim() });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["discussion-sources"] }),
        queryClient.invalidateQueries({ queryKey: ["discussion-rules"] }),
      ]);
      setSourceName(""); setRuleValue(""); setNotice("邮件列表和识别规则已提交，审核通过后会加入方式库。");
    } catch (value) { reportFailure(value); }
  }

  return <section style={section}>
    <div style={headerRow}>
      <div>
        <div style={headerTitleGroup}><div style={title}>技术探查</div>{badge && <span style={{ ...badgeStyle, color: badge.color, background: badge.background, borderColor: badge.border }}>{runChannel} · {badge.text}</span>}</div>
        <div style={subtitle}>统一探查邮件与 GitHub 技术讨论候选；候选内容需经过筛选、价值判断和整理，才会成为技术讨论条目。</div>
      </div>
      <div style={actions}>
        {expanded && <button type="button" onClick={resetPanelState} style={resetButton}>重置状态</button>}
        {canManage && technicalTab === "email" && mailRunActive && <button type="button" onClick={() => void stopCurrentRun()} disabled={stopBusy || run?.status === "stopping"} style={stopBusy || run?.status === "stopping" ? disabledDangerButton : stopButton}>{run?.status === "stopping" ? "邮件停止中…" : stopBusy ? "停止中…" : "停止邮件探查"}</button>}
        {canManage && technicalTab === "github" && githubRunActive && <button type="button" onClick={() => void stopCurrentRun()} disabled={stopBusy || run?.status === "stopping"} style={stopBusy || run?.status === "stopping" ? disabledDangerButton : stopButton}>{run?.status === "stopping" ? "GitHub 停止中…" : stopBusy ? "停止中…" : "停止 GitHub 探查"}</button>}
        <button type="button" onClick={() => setExpanded((value) => !value)} style={ghostButton}>{expanded ? "收起" : "展开"}</button>
      </div>
    </div>
    {error && <Notice tone="error" text={error} onClose={() => setError(null)} />}
    {notice && <Notice tone="success" text={notice} onClose={() => setNotice(null)} />}
    {!expanded ? null : <>
      <div style={technicalTabs}>
        <TechnicalTab active={technicalTab === "email"} onClick={() => onTechnicalTabChange("email")}>邮件探查</TechnicalTab>
        <TechnicalTab active={technicalTab === "github"} onClick={() => onTechnicalTabChange("github")}>GitHub 探查</TechnicalTab>
      </div>
      <div style={workspace}>
        <div style={technicalTab === "email" ? mailLeftColumn : leftColumn}>
          {technicalTab === "email" ? <>
          {canManage && <Panel title="邮箱连接" subtitle="凭据只从后端环境变量读取；此处不会显示或保存密码。" compact>
            {connections.data?.length ? <div style={connectionCard}>{connections.data.map((item) => <div key={item.id} style={connectionRow}><b>{item.name}</b><span>{item.host}:{item.port}/{item.folder} · {item.enabled ? "启用" : "停用"} · {item.health_status}</span>{item.last_error && <span style={{ color: "#b42318" }}>{item.last_error}</span>}</div>)}</div> : <form onSubmit={(event) => void saveConnection(event)} style={form}>
              <input required value={connection.name} onChange={(event) => setConnection({ ...connection, name: event.target.value })} placeholder="连接名称" style={input} />
              <input required value={connection.host} onChange={(event) => setConnection({ ...connection, host: event.target.value })} placeholder="IMAP 主机" style={input} />
              <div style={twoColumns}><input required type="number" value={connection.port} onChange={(event) => setConnection({ ...connection, port: event.target.value })} placeholder="端口" style={input} /><input required value={connection.folder} onChange={(event) => setConnection({ ...connection, folder: event.target.value })} placeholder="文件夹" style={input} /></div>
              <input required value={connection.username_env_key} onChange={(event) => setConnection({ ...connection, username_env_key: event.target.value })} placeholder="用户名环境变量键" style={input} />
              <input required value={connection.password_env_key} onChange={(event) => setConnection({ ...connection, password_env_key: event.target.value })} placeholder="密码环境变量键" style={input} />
              <button type="submit" style={primaryButton}>保存并启用邮箱</button>
            </form>}
          </Panel>}
          <Panel title="添加邮件列表与识别规则" subtitle="提交后不会立即收取，审核通过后会加入邮件探查方式库。">
            <form onSubmit={(event) => void addSource(event)} style={form}>
              <input required value={sourceName} onChange={(event) => setSourceName(event.target.value)} placeholder="邮件列表名称，例如：glibc libc-alpha" style={input} />
              <select value={ruleType} onChange={(event) => setRuleType(event.target.value)} style={input}><option value="list_id">List-Id</option><option value="list_post">List-Post</option><option value="delivered_to">Delivered-To</option><option value="to">To</option><option value="cc">Cc</option></select>
              <input required value={ruleValue} onChange={(event) => setRuleValue(event.target.value)} placeholder="识别值，例如：&lt;libc-alpha.sourceware.org&gt;" style={input} />
              <button type="submit" style={primaryButton}>加入待审核</button>
            </form>
          </Panel>
          <Panel title="运行控制" subtitle="收取入口在“技术探查 · 邮件探查”的邮件列表库。这里可查看每次收取的口径，以及已保存的全部讨论树。">
            <div style={runTabs}>
              <RunTab active={runView === "overview"} onClick={() => setRunView("overview")}>当前运行概览</RunTab>
              <RunTab active={runView === "all"} onClick={() => setRunView("all")}>全部讨论树 {counts.all}</RunTab>
              <RunTab active={runView === "published"} onClick={() => setRunView("published")}>已发布讨论 {counts.published}</RunTab>
              <RunTab active={runView === "waiting"} onClick={() => setRunView("waiting")}>待后续 {counts.waiting}</RunTab>
            </div>
            {runView === "overview" ? <div style={metrics}>
              <Metric label="本次扫描" value={`${totals.headers} 封`} detail="此前未处理的邮件头" />
              <Metric label="命中规则" value={`${totals.matched} 封`} detail={`未命中 ${totals.unknown} 封`} />
              <Metric label="新增入库" value={`${totals.inserted} 封`} detail="命中邮件中首次保存的邮件" />
              <Metric label="累计讨论树" value={String(counts.all)} detail={`已发布 ${counts.published} · 待后续 ${counts.waiting}`} />
            </div> : <DiscussionGroupList groups={visibleGroups} loading={groupPages.isLoading} failed={groupPages.isError} fetchingNextPage={groupPages.isFetchingNextPage} hasNextPage={groupPages.hasNextPage} onLoadMore={() => void groupPages.fetchNextPage()} view={runView} provider="mail" />}
            {run?.status === "failed" && <p style={errorText}>最近一次失败：{run.error_message || "未知错误"}</p>}
          </Panel>
          </> : <GitHubDiscoveryContent runLimitState={runLimitState} run={run} />}
        </div>
        <div style={rightColumn}>
          <div style={logBody}><DiscoveryLogPanel logs={logs} variant="technical" /></div>
        </div>
      </div>
    </>}
  </section>;
}

function Panel({ title, subtitle, children, compact = false }: { title: string; subtitle?: string; children: ReactNode; compact?: boolean }) { return <section style={compact ? compactPanel : panel}><div style={panelTitle}>{title}</div>{subtitle && <div style={panelSubtitle}>{subtitle}</div>}{children}</section>; }
function Metric({ label, value, detail }: { label: string; value: string; detail: string }) { return <div style={metric}><span style={metricLabel}>{label}</span><b style={metricValue}>{value}</b><span style={metricDetail}>{detail}</span></div>; }
function RunTab({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) { return <button type="button" onClick={onClick} style={active ? activeRunTab : runTab}>{children}</button>; }
function TechnicalTab({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) { return <button type="button" onClick={onClick} style={active ? activeTechnicalTab : technicalTab}>{children}</button>; }
function GitHubDiscoveryContent({ runLimitState, run }: { runLimitState: NewsRunFormState; run?: DiscussionPipelineRunState }) {
  const queryClient = useQueryClient();
  const [githubForm, setGithubForm] = useState({ owner: "", repo: "", display_name: "", token_env_key: "GITHUB_TOKEN", include_issues: true, include_discussions: true });
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runView, setRunView] = useState<RunView>("overview");
  const tokenEnvKey = githubForm.token_env_key.trim();
  const tokenStatus = useQuery({
    queryKey: ["discussion-github-token-status", tokenEnvKey],
    queryFn: () => fetchGitHubTokenStatus(tokenEnvKey),
    enabled: Boolean(tokenEnvKey),
    retry: false,
  });
  const captureLimit = buildManualNewsRunRequest(runLimitState);
  const captureWindowLabel = runLimitState.timeMode === "relative"
    ? `最近 ${runLimitState.relativeRange}`
    : runLimitState.startDate && runLimitState.endDate ? `${runLimitState.startDate} 至 ${runLimitState.endDate}` : "尚未设置";
  const repositoryRuns = run?.result?.github ?? [];
  const groupStatus: DiscussionGroupStatus = runView === "published" ? "published" : runView === "waiting" ? "waiting" : "all";
  const groupCounts = useQuery({
    queryKey: ["discussion-group-counts", "github"],
    queryFn: () => fetchDiscussionGroupCounts("github"),
    retry: false,
    refetchInterval: () => run?.status === "running" || run?.status === "stopping" ? 2000 : false,
  });
  const groupPages = useInfiniteQuery({
    queryKey: ["discussion-groups", "github", groupStatus],
    queryFn: ({ pageParam }) => fetchDiscussionGroups(groupStatus, pageParam, "github"),
    initialPageParam: 0,
    getNextPageParam: (page) => page.next_offset ?? undefined,
    retry: false,
  });
  const counts = groupCounts.data ?? { all: 0, published: 0, waiting: 0 };
  const visibleGroups = groupPages.data?.pages.flatMap((page) => page.groups) ?? [];
  const runTotals = repositoryRuns.reduce((summary, repository) => ({
    repositories: summary.repositories + 1,
    issueRoots: summary.issueRoots + (repository.issues?.roots ?? 0),
    issueComments: summary.issueComments + (repository.issues?.comments ?? 0),
    discussionRoots: summary.discussionRoots + (repository.discussions?.roots ?? 0),
    discussionReplies: summary.discussionReplies + (repository.discussions?.comments ?? 0) + (repository.discussions?.replies ?? 0),
    inserted: summary.inserted + (repository.issues?.inserted ?? 0) + (repository.discussions?.inserted ?? 0),
  }), { repositories: 0, issueRoots: 0, issueComments: 0, discussionRoots: 0, discussionReplies: 0, inserted: 0 });
  useEffect(() => {
    if (!notice && !error) return;
    const timeoutId = window.setTimeout(() => { setNotice(null); setError(null); }, 5000);
    return () => window.clearTimeout(timeoutId);
  }, [notice, error]);
  const report = (value: unknown) => setError(value instanceof ApiError ? value.message : "GitHub 探查操作失败，请稍后重试");
  async function create(event: FormEvent): Promise<void> {
    event.preventDefault(); setError(null); setNotice(null);
    if (!captureLimit) {
      setError("请先在页面顶部“抓取限制”中补全有效的时间窗口和条目上限。");
      return;
    }
    try {
      await createGitHubDiscussionRepository({ ...githubForm, owner: githubForm.owner.trim(), repo: githubForm.repo.trim(), display_name: githubForm.display_name.trim() || `${githubForm.owner.trim()}/${githubForm.repo.trim()}`, token_env_key: githubForm.token_env_key.trim(), time_window: captureLimit });
      await queryClient.invalidateQueries({ queryKey: ["discussion-github-repositories"] });
      setGithubForm({ owner: "", repo: "", display_name: "", token_env_key: "GITHUB_TOKEN", include_issues: true, include_discussions: true });
      setNotice(`仓库已提交；首次回填将使用页面抓取限制（${captureWindowLabel}）。审核通过后会加入方式库。`);
    } catch (value) { report(value); }
  }
  return <>
    {error && <Notice tone="error" text={error} onClose={() => setError(null)} />}
    {notice && <Notice tone="success" text={notice} onClose={() => setNotice(null)} />}
    <Panel title="GitHub 仓库" subtitle={`Issues 与 Discussions 都是候选入口；首次回填与首次定时探查统一使用页面顶部的抓取限制（当前：${captureWindowLabel}），不在仓库内单独设置时间范围。`}>
      <form onSubmit={(event) => void create(event)} style={form}>
        <div style={twoColumns}><input required value={githubForm.owner} onChange={(event) => setGithubForm({ ...githubForm, owner: event.target.value })} placeholder="owner，例如 containerd" style={input} /><input required value={githubForm.repo} onChange={(event) => setGithubForm({ ...githubForm, repo: event.target.value })} placeholder="repo，例如 containerd" style={input} /></div>
        <input value={githubForm.display_name} onChange={(event) => setGithubForm({ ...githubForm, display_name: event.target.value })} placeholder="展示名称（默认 owner/repo）" style={input} />
        <label style={credentialField}>
          <span style={credentialLabel}>后端 Token 环境变量名</span>
          <input required value={githubForm.token_env_key} onChange={(event) => setGithubForm({ ...githubForm, token_env_key: event.target.value })} placeholder="默认：GITHUB_TOKEN" style={input} />
        </label>
        <div style={tokenStatus?.data?.configured ? credentialReady : credentialMissing}>
          {tokenStatus.isLoading ? "正在检查后端凭据…" : tokenStatus.isError ? "无法确认后端凭据状态；请检查后端连接后重试。" : tokenStatus.data?.configured ? `已检测到后端环境变量「${tokenStatus.data.token_env_key}」。Token 已隐藏，不会写入数据库或显示在此处。` : `后端未检测到环境变量「${tokenEnvKey || "GITHUB_TOKEN"}」。请在 .env 配置后重启后端。`}
        </div>
        <div style={checkboxRow}><label><input type="checkbox" checked={githubForm.include_issues} onChange={(event) => setGithubForm({ ...githubForm, include_issues: event.target.checked })} /> Issues</label><label><input type="checkbox" checked={githubForm.include_discussions} onChange={(event) => setGithubForm({ ...githubForm, include_discussions: event.target.checked })} /> Discussions</label></div>
        <button type="submit" style={primaryButton} disabled={!githubForm.include_issues && !githubForm.include_discussions}>加入待审核仓库</button>
      </form>
    </Panel>
    <Panel title="运行控制" subtitle="探查入口在“技术探查 · GitHub 探查”的仓库库。这里查看本次同步口径，以及该仓库已保存的讨论树。">
      <div style={runTabs}>
        <RunTab active={runView === "overview"} onClick={() => setRunView("overview")}>当前运行概览</RunTab>
        <RunTab active={runView === "all"} onClick={() => setRunView("all")}>全部讨论树 {counts.all}</RunTab>
        <RunTab active={runView === "published"} onClick={() => setRunView("published")}>已发布讨论 {counts.published}</RunTab>
        <RunTab active={runView === "waiting"} onClick={() => setRunView("waiting")}>待后续 {counts.waiting}</RunTab>
      </div>
      {runView === "overview" ? <div style={metrics}>
        <Metric label="本次仓库" value={`${runTotals.repositories} 个`} detail={repositoryRuns.length ? "参与本次 GitHub 探查" : "尚未运行 GitHub 探查"} />
        <Metric label="读取 Issues" value={`${runTotals.issueRoots} 条`} detail={`评论 ${runTotals.issueComments} 条`} />
        <Metric label="读取 Discussions" value={`${runTotals.discussionRoots} 个`} detail={`评论与回复 ${runTotals.discussionReplies} 条`} />
        <Metric label="新增入库" value={`${runTotals.inserted} 条`} detail="首次保存的 GitHub 消息" />
      </div> : <DiscussionGroupList groups={visibleGroups} loading={groupPages.isLoading} failed={groupPages.isError} fetchingNextPage={groupPages.isFetchingNextPage} hasNextPage={groupPages.hasNextPage} onLoadMore={() => void groupPages.fetchNextPage()} view={runView} provider="github" />}
      {run?.status === "failed" && repositoryRuns.length > 0 && <p style={errorText}>最近一次失败：{run.error_message || "未知错误"}</p>}
    </Panel>
  </>;
}
function DiscussionGroupList({ groups, loading, failed, fetchingNextPage, hasNextPage, onLoadMore, view, provider }: { groups: DiscussionGroupSummary[]; loading: boolean; failed: boolean; fetchingNextPage: boolean; hasNextPage: boolean; onLoadMore: () => void; view: Exclude<RunView, "overview">; provider: "mail" | "github" }) {
  const emptyMessage = view === "published" ? "暂无已发布的技术讨论。" : view === "waiting" ? "暂无等待后续回复的讨论树。" : "暂无已建立的讨论树。";
  if (loading) return <div style={groupEmpty}>加载讨论树中…</div>;
  if (failed) return <div style={{ ...groupEmpty, color: "#b42318", borderColor: "#fecdca", background: "#fef3f2" }}>读取讨论树失败，请稍后刷新页面。</div>;
  if (!groups.length) return <div style={groupEmpty}>{emptyMessage}</div>;
  function handleScroll(event: UIEvent<HTMLDivElement>): void {
    const target = event.currentTarget;
    if (hasNextPage && !fetchingNextPage && target.scrollTop + target.clientHeight >= target.scrollHeight - 24) onLoadMore();
  }
  return <div style={groupList} onScroll={handleScroll}>{groups.map((group) => <DiscussionGroupRow key={group.id} group={group} provider={provider} />)}{fetchingNextPage && <div style={groupLoading}>正在加载更多讨论树…</div>}{hasNextPage && !fetchingNextPage && <div style={groupLoading}>继续向下滚动以加载更多</div>}</div>;
}
function DiscussionGroupRow({ group, provider }: { group: DiscussionGroupSummary; provider: "mail" | "github" }) {
  const state = groupState(group.processing_status);
  const explanation = group.summary || group.rejection_reason || (group.processing_status === "waiting" ? "等待更多有实质内容的回复后再次判断。" : "暂无补充说明。");
  return <div style={groupRow}>
    <div style={groupRowHead}><div style={groupTitle}>{group.title}</div><span style={{ ...groupStateBadge, color: state.color, background: state.background }}>{state.text}</span></div>
    <div style={groupMeta}>{group.message_count} {provider === "github" ? "条 GitHub 消息" : "封邮件"} · {group.participant_count} 位参与者 · 最近活动 {formatTime(group.last_activity_at)}</div>
    <div style={groupSummary}>{explanation}</div>
  </div>;
}
function groupState(status: DiscussionGroupSummary["processing_status"]) {
  if (status === "published") return { text: "已发布", color: "#027a48", background: "#ecfdf3" };
  if (status === "waiting") return { text: "待后续", color: "#b54708", background: "#fffaeb" };
  if (status === "failed") return { text: "整理失败", color: "#b42318", background: "#fef3f2" };
  return { text: "待整理", color: "#175cd3", background: "#eff6ff" };
}
function formatTime(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function Notice({ tone, text, onClose }: { tone: "success" | "error"; text: string; onClose: () => void }) { const success = tone === "success"; return <div style={{ display: "flex", gap: 8, alignItems: "flex-start", marginBottom: 12, padding: "9px 11px", borderRadius: 8, fontSize: 13, color: success ? "#067647" : "#b42318", background: success ? "#ecfdf3" : "#fef2f2", border: `1px solid ${success ? "#a3e0c4" : "#fca5a5"}` }}><span style={{ flex: 1 }}>{text}</span><button type="button" onClick={onClose} style={{ border: 0, background: "transparent", color: "inherit", cursor: "pointer", fontSize: 15 }}>×</button></div>; }

const section = { background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16, marginTop: 18 };
const headerRow = { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, marginBottom: 12, flexWrap: "wrap" as const };
const headerTitleGroup = { display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" as const };
const title = { fontSize: 15, fontWeight: 700, color: "#101828" };
const subtitle = { marginTop: 4, fontSize: 12, color: "#667085", maxWidth: 760 };
const actions = { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" as const };
const badgeStyle = { border: "1px solid", borderRadius: 999, padding: "3px 11px", fontSize: 12, fontWeight: 700 };
const workspace = { display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 14, alignItems: "stretch" };
const technicalTabs = { display: "flex", alignItems: "center", gap: 5, flexWrap: "wrap" as const, borderBottom: "1px solid #d0d5dd", marginBottom: 12 };
const technicalTab = { border: "none", borderBottom: "2px solid transparent", padding: "7px 10px", marginBottom: -1, background: "transparent", color: "#667085", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const activeTechnicalTab = { ...technicalTab, color: "#175cd3", borderBottomColor: "#175cd3" };
const leftColumn = { display: "grid", gap: 12, minWidth: 0 };
// Keep the technical log independent from the configuration column: new forms,
// repository rows, or notices on the left must not stretch the log panel.
const TECHNICAL_LOG_HEIGHT = 640;
const mailLeftColumn = { ...leftColumn, minHeight: TECHNICAL_LOG_HEIGHT, alignContent: "start" };
const rightColumn = { display: "flex", minWidth: 0, height: TECHNICAL_LOG_HEIGHT, alignSelf: "start" };
const panel = { border: "1px solid #eaecf0", borderRadius: 10, background: "#f8fafc", padding: 12 };
const compactPanel = { ...panel, padding: "5px 8px" };
const panelTitle = { fontSize: 13, fontWeight: 700, color: "#101828" };
const panelSubtitle = { fontSize: 12, color: "#667085", lineHeight: 1.5, margin: "4px 0 10px" };
const form = { display: "grid", gap: 8 };
const input = { border: "1px solid #d0d5dd", borderRadius: 7, padding: "8px 10px", color: "#101828", background: "#fff", fontSize: 13, width: "100%", boxSizing: "border-box" as const };
const twoColumns = { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 };
const primaryButton = { border: "none", borderRadius: 7, padding: "8px 12px", background: "#175cd3", color: "#fff", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const ghostButton = { border: "1px solid #d0d5dd", borderRadius: 999, padding: "8px 14px", background: "#fff", color: "#344054", fontSize: 13, fontWeight: 700, cursor: "pointer" };
const resetButton = { ...ghostButton, color: "#047857", borderColor: "#6ee7b7", background: "#ecfdf3" };
const stopButton = { ...ghostButton, color: "#b42318", borderColor: "#fecdca", background: "#fff" };
const disabledDangerButton = { ...stopButton, color: "#98a2b3", borderColor: "#eaecf0", cursor: "not-allowed" };
const connectionCard = { display: "grid", gap: 6 };
const connectionRow = { display: "grid", gap: 3, padding: "9px 10px", border: "1px solid #dfe6ee", background: "#fff", borderRadius: 8, fontSize: 12, color: "#475467" };
const metrics = { display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gridTemplateRows: "repeat(2, minmax(0, 1fr))", gap: 8, height: 148 };
const runTabs = { display: "flex", alignItems: "center", gap: 5, flexWrap: "wrap" as const, borderBottom: "1px solid #d0d5dd", marginBottom: 10 };
const runTab = { border: "none", borderBottom: "2px solid transparent", padding: "6px 8px", marginBottom: -1, background: "transparent", color: "#667085", fontSize: 11, fontWeight: 700, cursor: "pointer" };
const activeRunTab = { ...runTab, color: "#175cd3", borderBottomColor: "#175cd3" };
const metric = { display: "grid", gap: 2, padding: "9px 10px", border: "1px solid #e4ebf5", background: "#fff", borderRadius: 8 };
const metricLabel = { fontSize: 11, color: "#667085", fontWeight: 700 };
const metricValue = { color: "#101828", fontSize: 19 };
const metricDetail = { color: "#667085", fontSize: 11, whiteSpace: "nowrap" as const, overflow: "hidden", textOverflow: "ellipsis" };
const groupList = { display: "grid", alignContent: "start", gap: 7, height: 148, overflowY: "auto" as const, paddingRight: 2 };
const groupRow = { border: "1px solid #e4ebf5", borderRadius: 8, padding: "8px 9px", background: "#fff" };
const groupRowHead = { display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8 };
const groupTitle = { color: "#101828", fontSize: 12, fontWeight: 700, lineHeight: 1.4 };
const groupStateBadge = { flexShrink: 0, borderRadius: 999, padding: "2px 6px", fontSize: 10, fontWeight: 700 };
const groupMeta = { marginTop: 3, color: "#667085", fontSize: 10 };
const groupSummary = { marginTop: 4, color: "#475467", fontSize: 11, lineHeight: 1.45, display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" as const, overflow: "hidden" };
const groupEmpty = { height: 148, boxSizing: "border-box" as const, border: "1px dashed #d0d5dd", borderRadius: 8, padding: 12, color: "#667085", background: "#fff", fontSize: 11 };
const groupLoading = { color: "#667085", fontSize: 10, textAlign: "center" as const, padding: "2px 0" };
const logBody = { flex: 1, minHeight: 0 };
const errorText = { margin: "9px 0 0", color: "#b42318", fontSize: 12 };
const checkboxRow = { display: "flex", gap: 14, flexWrap: "wrap" as const, color: "#344054", fontSize: 12 };
const credentialField = { display: "grid", gap: 5 };
const credentialLabel = { color: "#344054", fontSize: 12, fontWeight: 700 };
const credentialReady = { color: "#027a48", fontSize: 12, lineHeight: 1.45 };
const credentialMissing = { color: "#b54708", fontSize: 12, lineHeight: 1.45 };
