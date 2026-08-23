import { useEffect, useMemo, useState, type CSSProperties } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import {
  approveGitHubDiscussionRepositories,
  deletePendingGitHubDiscussionRepositories,
  estimateGitHubDiscussionBackfill,
  fetchGitHubDiscussionRepositories,
  type GitHubDiscussionRepository,
} from "../discussions/api";

export function GitHubRepositoryReviewList() {
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [estimateBusyId, setEstimateBusyId] = useState<number | null>(null);
  const [estimates, setEstimates] = useState<Record<number, { issues: number; discussions: number }>>({});
  const [notice, setNotice] = useState<{ text: string; tone: "success" | "danger" } | null>(null);
  const repositories = useQuery({ queryKey: ["discussion-github-repositories"], queryFn: fetchGitHubDiscussionRepositories });
  const pending = useMemo(() => (repositories.data ?? []).filter((repository) => repository.review_status === "pending"), [repositories.data]);
  const selected = new Set(selectedIds);
  const allSelected = pending.length > 0 && pending.every((repository) => selected.has(repository.id));
  const isBusy = busy || estimateBusyId !== null;

  useEffect(() => {
    if (!notice) return;
    const timeoutId = window.setTimeout(() => setNotice(null), 5000);
    return () => window.clearTimeout(timeoutId);
  }, [notice]);

  async function refresh(): Promise<void> {
    await repositories.refetch();
  }

  function toggle(id: number, checked: boolean): void {
    setSelectedIds((current) => checked ? [...new Set([...current, id])] : current.filter((value) => value !== id));
  }

  async function approve(): Promise<void> {
    if (!selectedIds.length) { setNotice({ text: "请先选择至少一个待审核 GitHub 仓库。", tone: "danger" }); return; }
    setNotice(null); setBusy(true);
    try {
      const result = await approveGitHubDiscussionRepositories(selectedIds);
      setSelectedIds([]); setNotice({ text: `已通过 ${result.approved_count} 个 GitHub 仓库。`, tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "审核通过失败", tone: "danger" }); } finally { setBusy(false); }
  }

  async function remove(): Promise<void> {
    if (!selectedIds.length) { setNotice({ text: "请先选择至少一个待审核 GitHub 仓库。", tone: "danger" }); return; }
    if (!window.confirm(`删除选中的 ${selectedIds.length} 个待审核 GitHub 仓库？`)) return;
    setNotice(null); setBusy(true);
    try {
      const result = await deletePendingGitHubDiscussionRepositories(selectedIds);
      setSelectedIds([]); setNotice({ text: `已删除 ${result.deleted_count} 个待审核 GitHub 仓库。`, tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "删除失败", tone: "danger" }); } finally { setBusy(false); }
  }

  async function estimate(repository: GitHubDiscussionRepository): Promise<void> {
    setNotice(null); setEstimateBusyId(repository.id);
    try {
      const result = await estimateGitHubDiscussionBackfill(repository.id);
      setEstimates((current) => ({ ...current, [repository.id]: result }));
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "预估回填失败", tone: "danger" }); } finally { setEstimateBusyId(null); }
  }

  return <section style={section}>
    <div style={headerRow}>
      <div>
        <div style={title}>GitHub 探查 · 待审核仓库</div>
        <div style={subtitle}>新增仓库先在这里预估首次回填规模，再确认启用；Token 只保留环境变量引用。</div>
      </div>
      <div style={actions}>
        <span style={counter}>待审核 <b>{pending.length}</b> 个 · 已选 <b>{selected.size}</b> 个</span>
        <label style={checkLabel}><input type="checkbox" checked={allSelected} disabled={!pending.length || isBusy} onChange={(event) => setSelectedIds(event.target.checked ? pending.map((repository) => repository.id) : [])} />全选</label>
        <button type="button" disabled={!selectedIds.length || isBusy} onClick={() => void approve()} style={!selectedIds.length || isBusy ? disabledPrimary : primary}>{busy ? "通过中…" : "批量通过"}</button>
        <button type="button" disabled={!selectedIds.length || isBusy} onClick={() => void remove()} style={!selectedIds.length || isBusy ? disabledDanger : danger}>{busy ? "删除中…" : "批量删除"}</button>
      </div>
    </div>
    {notice && <div style={{ ...noticeStyle, display: "flex", gap: 8, alignItems: "flex-start", color: notice.tone === "danger" ? "#b42318" : "#067647", borderColor: notice.tone === "danger" ? "#fecdca" : "#abefc6", background: notice.tone === "danger" ? "#fef3f2" : "#ecfdf3" }}><span style={{ flex: 1 }}>{notice.text}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示" style={closeButton}>×</button></div>}
    <div style={list}>
      {repositories.isLoading && <div style={empty}>加载待审核 GitHub 仓库中…</div>}
      {repositories.isError && <div style={{ ...empty, color: "#b42318", borderColor: "#fecdca", background: "#fef3f2" }}>加载待审核 GitHub 仓库失败。</div>}
      {pending.map((repository) => <RepositoryRow key={repository.id} repository={repository} estimate={estimates[repository.id]} selected={selected.has(repository.id)} busy={isBusy} onToggle={(checked) => toggle(repository.id, checked)} onEstimate={() => void estimate(repository)} />)}
      {repositories.data && !pending.length && <div style={empty}>暂无待审核 GitHub 仓库。</div>}
    </div>
  </section>;
}

function RepositoryRow({ repository, estimate, selected, busy, onToggle, onEstimate }: { repository: GitHubDiscussionRepository; estimate?: { issues: number; discussions: number }; selected: boolean; busy: boolean; onToggle: (checked: boolean) => void; onEstimate: () => void }) {
  const kinds = [repository.include_issues && "Issues", repository.include_discussions && "Discussions"].filter(Boolean).join(" · ");
  return <div style={{ ...row, borderColor: selected ? "#b9d4ff" : "#eaecf0", background: selected ? "#f8fbff" : "#fff" }}><input type="checkbox" aria-label={`选择 ${repository.display_name}`} checked={selected} disabled={busy} onChange={(event) => onToggle(event.target.checked)} /><div style={{ flex: 1, minWidth: 0 }}><div style={rowTitle}>{repository.display_name}<span style={pendingBadge}>pending</span></div><div style={meta}>{kinds} · {repository.token_configured ? `凭据已就绪（${repository.token_env_key}）` : `未检测到凭据（${repository.token_env_key}）`}</div>{estimate && <div style={estimateStyle}>首次回填预估：Issues {estimate.issues} · Discussions {estimate.discussions}</div>}</div><div style={rowActions}><button type="button" disabled={busy} onClick={onEstimate} style={busy ? disabledButton : button}>{busy ? "预估中…" : "预估回填"}</button></div></div>;
}

const section: CSSProperties = { background: "#fff", border: "1px solid #d0d5dd", borderRadius: 10, padding: 16, marginTop: 14 };
const headerRow: CSSProperties = { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap", marginBottom: 12 };
const title: CSSProperties = { fontSize: 13, fontWeight: 700, color: "#101828" };
const subtitle: CSSProperties = { fontSize: 11, color: "#667085", marginTop: 3 };
const actions: CSSProperties = { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" };
const counter: CSSProperties = { fontSize: 11, color: "#475467" };
const checkLabel: CSSProperties = { display: "inline-flex", gap: 5, alignItems: "center", color: "#475467", fontSize: 11 };
const primary: CSSProperties = { border: "none", borderRadius: 999, padding: "8px 16px", background: "#175cd3", color: "#fff", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const danger: CSSProperties = { border: "1px solid #fecdca", borderRadius: 999, padding: "8px 16px", background: "#fff", color: "#b42318", fontSize: 12, fontWeight: 700, cursor: "pointer" };
const disabledPrimary: CSSProperties = { ...primary, background: "#98a2b3", cursor: "not-allowed" };
const disabledDanger: CSSProperties = { ...danger, color: "#98a2b3", borderColor: "#eaecf0", cursor: "not-allowed" };
const noticeStyle: CSSProperties = { border: "1px solid", borderRadius: 8, padding: "8px 10px", fontSize: 11, marginBottom: 10 };
const closeButton: CSSProperties = { border: 0, background: "transparent", color: "inherit", cursor: "pointer", fontSize: 16, lineHeight: 1, padding: 0 };
const list: CSSProperties = { display: "grid", gap: 8 };
const row: CSSProperties = { display: "flex", alignItems: "flex-start", gap: 10, border: "1px solid", borderRadius: 9, padding: "9px 11px" };
const rowTitle: CSSProperties = { fontSize: 13, fontWeight: 700, color: "#101828" };
const pendingBadge: CSSProperties = { marginLeft: 6, borderRadius: 999, padding: "2px 7px", background: "#fffaeb", color: "#b54708", fontSize: 11 };
const meta: CSSProperties = { marginTop: 5, color: "#475467", fontSize: 11, lineHeight: 1.5 };
const estimateStyle: CSSProperties = { marginTop: 4, color: "#175cd3", fontSize: 11, fontWeight: 700 };
const rowActions: CSSProperties = { display: "flex", gap: 6, flexWrap: "wrap", alignSelf: "center" };
const button: CSSProperties = { border: "1px solid #b9d4ff", borderRadius: 6, padding: "6px 9px", background: "#fff", color: "#175cd3", fontSize: 11, fontWeight: 700, cursor: "pointer" };
const disabledButton: CSSProperties = { ...button, color: "#98a2b3", borderColor: "#eaecf0", cursor: "not-allowed" };
const empty: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 14, color: "#667085", fontSize: 12 };
