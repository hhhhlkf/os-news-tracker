import { useMemo, useState, type CSSProperties } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../api/client";
import {
  deleteDiscussionSource,
  fetchDiscussionConnections,
  fetchDiscussionRules,
  fetchDiscussionSources,
  fetchLatestDiscussionPipelineRun,
  patchDiscussionSource,
  runDiscussionPipeline,
  stopDiscussionPipeline,
  type DiscussionPipelineRun,
} from "../discussions/api";

export function DiscussionSourceLibraryList({ onRunStarted, canManage }: { onRunStarted: (run: DiscussionPipelineRun) => void; canManage: boolean }) {
  const queryClient = useQueryClient();
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ text: string; tone: "success" | "danger" } | null>(null);
  const sources = useQuery({ queryKey: ["discussion-sources"], queryFn: fetchDiscussionSources });
  const rules = useQuery({ queryKey: ["discussion-rules"], queryFn: fetchDiscussionRules });
  const connections = useQuery({ queryKey: ["discussion-connections"], queryFn: fetchDiscussionConnections });
  const latestRun = useQuery({ queryKey: ["discussion-latest-run"], queryFn: fetchLatestDiscussionPipelineRun, refetchInterval: (query) => ["running", "stopping"].includes(query.state.data?.status ?? "") ? 2000 : false });
  const library = useMemo(() => (sources.data ?? []).filter((source) => source.review_status === "approved"), [sources.data]);
  const selected = new Set(selectedIds);
  const allSelected = library.length > 0 && library.every((source) => selected.has(source.id));
  const selectedActiveIds = library.filter((source) => selected.has(source.id) && source.enabled).map((source) => source.id);
  const activeConnection = connections.data?.some((connection) => connection.enabled);
  const running = ["running", "stopping"].includes(latestRun.data?.status ?? "");
  const mailRunning = running && latestRun.data?.source_ids !== null && latestRun.data?.github_repository_ids === null;

  async function refresh(): Promise<void> {
    await Promise.all([
      sources.refetch(), rules.refetch(), connections.refetch(), latestRun.refetch(),
      queryClient.invalidateQueries({ queryKey: ["discussion-latest-run"] }),
    ]);
  }

  function toggle(id: number, checked: boolean): void {
    setSelectedIds((current) => checked ? [...new Set([...current, id])] : current.filter((value) => value !== id));
  }

  async function collectSelected(): Promise<void> {
    if (!selectedActiveIds.length) { setNotice({ text: "请先选择至少一个已启用的邮件列表。", tone: "danger" }); return; }
    setNotice(null); setBusy(true);
    try {
      const run = await runDiscussionPipeline("mail", selectedActiveIds);
      onRunStarted(run);
      setNotice({ text: "已开始收取新邮件并整理选中的邮件列表；过程请在邮件探查运行控制区查看。", tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "启动收取失败", tone: "danger" }); } finally { setBusy(false); }
  }

  async function stopCurrentRun(): Promise<void> {
    const run = latestRun.data;
    if (!run || !mailRunning) return;
    setNotice(null); setBusy(true);
    try {
      await stopDiscussionPipeline(run.id);
      setNotice({ text: "已请求停止邮件探查；当前网络或模型调用结束后将停止后续处理。", tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "停止邮件探查失败", tone: "danger" }); } finally { setBusy(false); }
  }

  async function removeSelected(): Promise<void> {
    if (!selectedIds.length) { setNotice({ text: "请先选择至少一个邮件列表。", tone: "danger" }); return; }
    if (!window.confirm(`删除选中的 ${selectedIds.length} 个邮件列表？已收取的邮件历史会保留，但它们将不再参与匹配。`)) return;
    setNotice(null); setBusy(true);
    try {
      for (const sourceId of selectedIds) await deleteDiscussionSource(sourceId);
      setSelectedIds([]); setNotice({ text: "已从邮件列表库移除选中的邮件列表。", tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "删除失败", tone: "danger" }); } finally { setBusy(false); }
  }

  async function setSelectedEnabled(enabled: boolean): Promise<void> {
    if (!selectedIds.length) { setNotice({ text: "请先选择至少一个邮件列表。", tone: "danger" }); return; }
    setNotice(null); setBusy(true);
    try {
      for (const sourceId of selectedIds) await patchDiscussionSource(sourceId, enabled);
      setNotice({ text: enabled ? "已启用选中的邮件列表。" : "已禁用选中的邮件列表，不会再匹配新邮件。", tone: "success" });
      await refresh();
    } catch (error) { setNotice({ text: error instanceof ApiError ? error.message : "更新启用状态失败", tone: "danger" }); } finally { setBusy(false); }
  }

  return <section style={section}>
    <div style={headerRow}>
      <div><div style={title}>邮件探查 · 邮件列表库</div><div style={subtitle}>按顺序收取已选邮件列表，可随时在邮件探查运行控制区查看过程和结果。</div></div>
      <div style={actions}>
        {canManage && <><span style={counter}>已选 <b>{selected.size}</b> 个</span>
        <label style={checkLabel}><input type="checkbox" checked={allSelected} disabled={!library.length || busy || running} onChange={(event) => setSelectedIds(event.target.checked ? library.map((source) => source.id) : [])} />全选</label>
        <button type="button" disabled={!selectedActiveIds.length || busy || running || !activeConnection} onClick={() => void collectSelected()} style={!selectedActiveIds.length || busy || running || !activeConnection ? disabledPrimary : primary}>{mailRunning ? "收取中…" : "收取选中"}</button>
        {mailRunning && <button type="button" disabled={busy || latestRun.data?.status === "stopping"} onClick={() => void stopCurrentRun()} style={busy || latestRun.data?.status === "stopping" ? disabledDanger : danger}>{latestRun.data?.status === "stopping" ? "停止中…" : "停止邮件探查"}</button>}
        <button type="button" disabled={!selectedIds.length || busy || running} onClick={() => void setSelectedEnabled(true)} style={!selectedIds.length || busy || running ? disabledPrimary : primary}>启用选中</button>
        <button type="button" disabled={!selectedIds.length || busy || running} onClick={() => void setSelectedEnabled(false)} style={!selectedIds.length || busy || running ? disabledDanger : danger}>禁用选中</button>
        <button type="button" disabled={!selectedIds.length || busy || running} onClick={() => void removeSelected()} style={!selectedIds.length || busy || running ? disabledDanger : danger}>{busy ? "处理中…" : "批量删除列表"}</button></>}
      </div>
    </div>
    {notice && <div style={{ ...noticeStyle, color: notice.tone === "danger" ? "#b42318" : "#067647", borderColor: notice.tone === "danger" ? "#fecdca" : "#abefc6", background: notice.tone === "danger" ? "#fef3f2" : "#ecfdf3" }}>{notice.text}</div>}
    {!activeConnection && <div style={warning}>请先在“邮件探查 · 运行控制”配置并启用专用邮箱连接。</div>}
    <div style={list}>
      {sources.isLoading && <div style={empty}>加载邮件列表库中...</div>}
      {sources.isError && <div style={{ ...empty, color: "#b42318", borderColor: "#fecdca", background: "#fef3f2" }}>加载邮件列表库失败。</div>}
      {library.map((source) => <SourceRow key={source.id} source={source} rules={rules.data ?? []} selected={selected.has(source.id)} busy={busy || running} selectable={canManage} onToggle={(checked) => toggle(source.id, checked)} />)}
      {sources.data && !library.length && <div style={empty}>暂无已通过审核的邮件列表。</div>}
    </div>
  </section>;
}

function SourceRow({ source, rules, selected, busy, selectable, onToggle }: { source: { id: number; name: string; enabled: boolean }; rules: Array<{ source_id: number; rule_type: string; match_value: string }>; selected: boolean; busy: boolean; selectable: boolean; onToggle: (checked: boolean) => void }) {
  const sourceRules = rules.filter((rule) => rule.source_id === source.id);
  return <div style={{ ...row, borderColor: selected ? "#b9d4ff" : "#eaecf0", background: selected ? "#f8fbff" : "#fff" }}>{selectable && <input type="checkbox" aria-label={`选择 ${source.name}`} checked={selected} disabled={busy} onChange={(event) => onToggle(event.target.checked)} />}<div style={{ flex: 1, minWidth: 0 }}><div style={{ ...rowTitle, color: source.enabled ? "#101828" : "#98a2b3" }}>{source.name}<span style={source.enabled ? activeBadge : disabledBadge}>{source.enabled ? "active" : "disabled"}</span></div><div style={rulesStyle}>{sourceRules.map((rule) => `${rule.rule_type}: ${rule.match_value}`).join(" · ")}</div></div><div style={rowStatus}>{source.enabled ? "已启用" : "已禁用"}<br />{sourceRules.length} 条识别规则</div></div>;
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
const warning: CSSProperties = { color: "#b54708", background: "#fffaeb", border: "1px solid #fedf89", borderRadius: 8, padding: "8px 10px", fontSize: 11, marginBottom: 10 };
const list: CSSProperties = { display: "grid", gap: 8 };
const row: CSSProperties = { display: "flex", alignItems: "center", gap: 12, border: "1px solid", borderRadius: 9, padding: "9px 11px" };
const rowTitle: CSSProperties = { fontSize: 13, fontWeight: 700, color: "#101828" };
const activeBadge: CSSProperties = { marginLeft: 6, borderRadius: 999, padding: "2px 7px", background: "#ecfdf3", color: "#027a48", fontSize: 11 };
const disabledBadge: CSSProperties = { ...activeBadge, background: "#f2f4f7", color: "#667085" };
const rulesStyle: CSSProperties = { marginTop: 5, color: "#475467", fontSize: 11, wordBreak: "break-word" };
const rowStatus: CSSProperties = { minWidth: 180, textAlign: "right", color: "#475467", fontSize: 11, lineHeight: 1.5 };
const empty: CSSProperties = { border: "1px dashed #d0d5dd", borderRadius: 8, padding: 14, color: "#667085", fontSize: 12 };
