import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import {
  deleteGitHubDiscussionRepository,
  fetchGitHubDiscussionRepositories,
  fetchLatestDiscussionPipelineRun,
  patchGitHubDiscussionRepository,
  runDiscussionPipeline,
  stopDiscussionPipeline,
  type DiscussionPipelineRun,
} from "../discussions/api";

export function GitHubRepositoryLibraryList({ onRunStarted, canManage }: { onRunStarted: (run: DiscussionPipelineRun) => void; canManage: boolean }) {
  const queryClient = useQueryClient();
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ text: string; tone: "success" | "danger" } | null>(null);
  const repositories = useQuery({ queryKey: ["discussion-github-repositories"], queryFn: fetchGitHubDiscussionRepositories });
  const latestRun = useQuery({ queryKey: ["discussion-latest-run"], queryFn: fetchLatestDiscussionPipelineRun, refetchInterval: (query) => ["running", "stopping"].includes(query.state.data?.status ?? "") ? 2000 : false });
  const library = useMemo(() => (repositories.data ?? []).filter((repository) => repository.review_status === "approved"), [repositories.data]);
  const selected = new Set(selectedIds);
  const activeIds = library.filter((repository) => selected.has(repository.id) && repository.enabled).map((repository) => repository.id);
  const running = ["running", "stopping"].includes(latestRun.data?.status ?? "");
  const githubRunning = running && latestRun.data?.github_repository_ids !== null && latestRun.data?.source_ids === null;
  const disabled = busy || running;

  useEffect(() => {
    if (!notice) return;
    const timeoutId = window.setTimeout(() => setNotice(null), 5000);
    return () => window.clearTimeout(timeoutId);
  }, [notice]);

  async function refresh(): Promise<void> {
    await Promise.all([repositories.refetch(), latestRun.refetch(), queryClient.invalidateQueries({ queryKey: ["discussion-github-repositories"] })]);
  }

  async function runSelected(): Promise<void> {
    if (!activeIds.length) { setNotice({ text: "请先选择至少一个已启用的 GitHub 仓库。", tone: "danger" }); return; }
    setNotice(null); setBusy(true);
    try {
      const run = await runDiscussionPipeline("github", undefined, activeIds);
      onRunStarted(run);
      setNotice({ text: "已启动 GitHub 技术探查；运行日志在“技术探查”右侧统一显示。", tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "启动 GitHub 探查失败", tone: "danger" }); } finally { setBusy(false); }
  }

  async function stopCurrentRun(): Promise<void> {
    const run = latestRun.data;
    if (!run || !githubRunning) return;
    setNotice(null); setBusy(true);
    try {
      await stopDiscussionPipeline(run.id);
      setNotice({ text: "已请求停止 GitHub 探查；当前网络或模型调用结束后将停止后续处理。", tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "停止 GitHub 探查失败", tone: "danger" }); } finally { setBusy(false); }
  }

  async function setEnabled(enabled: boolean): Promise<void> {
    if (!selectedIds.length) { setNotice({ text: "请先选择至少一个 GitHub 仓库。", tone: "danger" }); return; }
    setNotice(null); setBusy(true);
    try {
      await Promise.all(selectedIds.map((repositoryId) => patchGitHubDiscussionRepository(repositoryId, enabled)));
      setNotice({ text: enabled ? "已启用选中的 GitHub 仓库。" : "已停用选中的 GitHub 仓库。", tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "更新 GitHub 仓库状态失败", tone: "danger" }); } finally { setBusy(false); }
  }

  async function removeSelected(): Promise<void> {
    if (!selectedIds.length) { setNotice({ text: "请先选择至少一个 GitHub 仓库。", tone: "danger" }); return; }
    if (!window.confirm(`删除选中的 ${selectedIds.length} 个 GitHub 仓库？仓库配置、同步记录和来源关联会移除，已收录的讨论与 Item 历史会保留。`)) return;
    setNotice(null); setBusy(true);
    try {
      await Promise.all(selectedIds.map((repositoryId) => deleteGitHubDiscussionRepository(repositoryId)));
      setSelectedIds([]);
      setNotice({ text: "已删除选中的 GitHub 仓库；历史讨论和 Item 已保留。", tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "删除 GitHub 仓库失败", tone: "danger" }); } finally { setBusy(false); }
  }

  return <section style={section}>
    <div style={header}><div><div style={title}>GitHub 探查 · 仓库</div><div style={subtitle}>已审核仓库在这里统一启停与手动探查；定时同步会沿用其水位增量扫描。</div></div><div style={actions}>{canManage && <><span style={counter}>已选 <b>{selected.size}</b> 个</span><label style={checkLabel}><input type="checkbox" checked={library.length > 0 && library.every((repository) => selected.has(repository.id))} disabled={!library.length || disabled} onChange={(event) => setSelectedIds(event.target.checked ? library.map((repository) => repository.id) : [])} />全选</label><button type="button" disabled={!activeIds.length || disabled} onClick={() => void runSelected()} style={!activeIds.length || disabled ? disabledPrimary : primary}>{githubRunning ? "探查中…" : "探查选中"}</button>{githubRunning && <button type="button" disabled={busy || latestRun.data?.status === "stopping"} onClick={() => void stopCurrentRun()} style={busy || latestRun.data?.status === "stopping" ? disabledDanger : danger}>{latestRun.data?.status === "stopping" ? "停止中…" : "停止 GitHub 探查"}</button>}<button type="button" disabled={!selectedIds.length || disabled} onClick={() => void setEnabled(true)} style={!selectedIds.length || disabled ? disabledPrimary : primary}>启用选中</button><button type="button" disabled={!selectedIds.length || disabled} onClick={() => void setEnabled(false)} style={!selectedIds.length || disabled ? disabledDanger : danger}>禁用选中</button><button type="button" disabled={!selectedIds.length || disabled} onClick={() => void removeSelected()} style={!selectedIds.length || disabled ? disabledDanger : danger}>删除选中</button></>}</div></div>
    {notice && <Notice tone={notice.tone} text={notice.text} onClose={() => setNotice(null)} />}
    <div style={list}>{repositories.isLoading && <div style={empty}>加载 GitHub 仓库库中…</div>}{repositories.isError && <div style={{ ...empty, color: "#b42318", borderColor: "#fecdca", background: "#fef3f2" }}>加载 GitHub 仓库库失败。</div>}{library.map((repository) => <div key={repository.id} style={row}>{canManage && <input type="checkbox" checked={selected.has(repository.id)} disabled={disabled} onChange={(event) => setSelectedIds((current) => event.target.checked ? [...new Set([...current, repository.id])] : current.filter((id) => id !== repository.id))} />}<div style={{ flex: 1, minWidth: 0 }}><div style={rowTitle}>{repository.display_name}<span style={repository.enabled ? activeBadge : disabledBadge}>{repository.enabled ? "active" : "disabled"}</span></div><div style={meta}>{[repository.include_issues && "Issues", repository.include_discussions && "Discussions"].filter(Boolean).join(" · ")} · {repository.token_configured ? "后端凭据已就绪" : "后端未检测到凭据"}</div>{repository.last_error && <div style={errorText}>{repository.last_error}</div>}</div></div>)}{repositories.data && !library.length && <div style={empty}>暂无已通过审核的 GitHub 仓库。</div>}</div>
  </section>;
}

function Notice({ tone, text, onClose }: { tone: "success" | "danger"; text: string; onClose: () => void }) {
  const isDanger = tone === "danger";
  return <div style={{ ...noticeStyle, display: "flex", gap: 8, alignItems: "flex-start", color: isDanger ? "#b42318" : "#067647", borderColor: isDanger ? "#fecdca" : "#abefc6", background: isDanger ? "#fef3f2" : "#ecfdf3" }}><span style={{ flex: 1 }}>{text}</span><button type="button" onClick={onClose} aria-label="关闭提示" style={closeButton}>×</button></div>;
}

const section: CSSProperties = { background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16, marginTop: 14 };
const header: CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 10, flexWrap: "wrap", marginBottom: 12 };
const title: CSSProperties = { fontSize: 13, fontWeight: 700, color: "#101828" };
const subtitle: CSSProperties = { fontSize: 11, color: "#667085", marginTop: 3 };
const actions: CSSProperties = { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" };
const counter: CSSProperties = { fontSize: 11, color: "#475467" };
const checkLabel: CSSProperties = { display: "inline-flex", gap: 5, alignItems: "center", color: "#475467", fontSize: 11 };
const noticeStyle: CSSProperties = { border: "1px solid", borderRadius: 8, padding: "8px 10px", fontSize: 11, marginBottom: 10 };
const closeButton: CSSProperties = { border: 0, background: "transparent", color: "inherit", cursor: "pointer", fontSize: 16, lineHeight: 1, padding: 0 };
const list: CSSProperties = { display: "grid", gap: 8 };
const row: CSSProperties = { display: "flex", alignItems: "center", gap: 10, border: "1px solid #eaecf0", borderRadius: 9, padding: "9px 11px" };
const rowTitle: CSSProperties = { fontSize: 13, fontWeight: 700, color: "#101828" };
const activeBadge: CSSProperties = { marginLeft: 6, borderRadius: 999, padding: "2px 7px", background: "#ecfdf3", color: "#027a48", fontSize: 11 };
const disabledBadge: CSSProperties = { ...activeBadge, background: "#f2f4f7", color: "#667085" };
const meta: CSSProperties = { marginTop: 5, color: "#475467", fontSize: 11 };
const errorText: CSSProperties = { marginTop: 4, color: "#b42318", fontSize: 11 };
const primary: CSSProperties = { border: "none", borderRadius: 999, padding: "8px 14px", background: "#175cd3", color: "#fff", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const danger: CSSProperties = { border: "1px solid #fecdca", borderRadius: 999, padding: "8px 14px", background: "#fff", color: "#b42318", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const disabledPrimary: CSSProperties = { ...primary, background: "#98a2b3", cursor: "not-allowed" };
const disabledDanger: CSSProperties = { ...danger, color: "#98a2b3", borderColor: "#eaecf0", cursor: "not-allowed" };
const empty: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 14, color: "#667085", fontSize: 12 };
